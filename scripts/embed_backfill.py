#!/usr/bin/env python3
"""Досчитать векторы (эмбеддинги) кускам памяти встреч, у которых их нет.

Зачем. Куски встреч (`meeting_chunks`) получают вектор при индексации — но
только если эмбеддинги уже были включены (`VTX_EMBED_PROVIDER`). Всё, что
проиндексировано раньше (архив через scripts/index_archive.py, задачи до
включения), лежит без вектора и в векторный поиск не попадает; то же — куски,
у которых провайдер в момент индексации не ответил. Этот скрипт добирает их.

Запуск на сервере (внутри контейнера, под пользователем приложения):

    # 1. План — ничего не считает: провайдер, размерность, сколько кусков без вектора
    docker compose exec -u app app python scripts/embed_backfill.py --user ЛОГИН
    # 2. Пачка на пробу
    docker compose exec -u app app python scripts/embed_backfill.py --user ЛОГИН --apply --limit 50
    # 3. Всё остальное
    docker compose exec -u app app python scripts/embed_backfill.py --user ЛОГИН --apply

`--user` — логин владельца команды: берутся куски ЭТОЙ команды и её ключ
провайдера (из интерфейса, иначе из .env). Повторный прогон безопасен:
куски с вектором не трогаются. Если колонка `embedding` создана под другую
размерность (сменили провайдера или VTX_EMBED_DIM) — скрипт остановится и
скажет, что колонку надо снести; после `ALTER TABLE meeting_chunks DROP
COLUMN embedding` всё пересчитывается этим же скриптом.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, embeddings, security  # noqa: E402


def probe_dim(provider) -> int:
    """Размерность вектора: из настроек/провайдера, у Ollama — по пробному
    запросу (модель сама решает, сколько измерений отдать)."""
    if provider.dim():
        return provider.dim()
    vecs = provider.embed_texts(["проба размерности"], embeddings.KIND_DOC)
    return len(vecs[0]) if vecs and vecs[0] else 0


def backfill(team: str, provider, limit: int | None, batch: int,
             out=print) -> tuple[int, int]:
    """Досчитать векторы пачками. Возвращает (записано, пропущено без вектора).
    Останавливается, когда кусков без вектора не осталось, достигнут `limit`
    или пачка целиком не посчиталась (провайдер лежит — крутиться нет смысла)."""
    done, skipped = 0, 0
    seen: set[str] = set()
    while limit is None or done < limit:
        take = batch if limit is None else min(batch, limit - done)
        rows = db.chunks_without_embedding(team, limit=take + skipped)
        rows = [r for r in rows if r["id"] not in seen][:take]
        if not rows:
            break
        for r in rows:
            seen.add(r["id"])
        t0 = time.time()
        vecs = provider.embed_texts([r["text"] for r in rows], embeddings.KIND_DOC)
        pairs = [(r["id"], v) for r, v in zip(rows, vecs) if v]
        if not pairs:
            out("  ✗ пачка не посчиталась целиком — провайдер не отвечает, останавливаюсь")
            break
        n = db.chunks_set_embeddings(pairs)
        done += n
        skipped += len(rows) - n
        out(f"  ✓ {done} векторов записано"
            + (f", без вектора {skipped}" if skipped else "")
            + f" ({time.time() - t0:.1f} с на пачку)")
    return done, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True, help="логин владельца команды")
    ap.add_argument("--apply", action="store_true", help="считать (без него — только план)")
    ap.add_argument("--limit", type=int, default=None, help="не больше N кусков")
    ap.add_argument("--batch", type=int, default=embeddings.BATCH,
                    help=f"кусков в пачке (по умолчанию {embeddings.BATCH})")
    args = ap.parse_args(argv)

    if not db.enabled():
        print("Нужен Postgres (DATABASE_URL): память встреч живёт только в базе.")
        return 2
    if not embeddings.enabled():
        print("Эмбеддинги выключены: задайте VTX_EMBED_PROVIDER=yandex|gemini|ollama "
              "(и ключ провайдера) в .env.")
        return 2
    team = security.team_of(args.user)
    provider = embeddings.provider_for(team)
    if provider is None:
        print(f"Провайдер {config.EMBED_PROVIDER}: нет ключа ни у команды {team}, "
              "ни в .env — считать нечем.")
        return 2
    dim = probe_dim(provider)
    if not dim:
        print(f"Провайдер {provider.name} ({provider.model or 'модель по умолчанию'}) "
              "не ответил на пробный запрос — смотрите лог vtx.embeddings.")
        return 1
    if not db.vector_ready(dim):
        print(f"База не готова к векторам размерности {dim}: нет pgvector (образ "
              "pgvector/pgvector:pg16) или колонка embedding создана под другую "
              "размерность — подробности в логе vtx.db.")
        return 1
    st = db.chunks_embedding_stats(team)
    todo = st["chunks"] - st["embedded"]
    print(f"Команда: {team}. Провайдер: {provider.name}"
          + (f" ({provider.model})" if provider.model else "") + f", {dim} измерений.")
    print(f"Кусков всего: {st['chunks']}, с вектором: {st['embedded']}, досчитать: {todo}"
          + (f" (в этот прогон — не больше {args.limit})" if args.limit is not None else ""))
    if not args.apply:
        print("\nЭто план — ничего не посчитано. Запустите с --apply (на пробу — с --limit 50).")
        return 0
    if not todo:
        print("Досчитывать нечего.")
        return 0
    done, skipped = backfill(team, provider, args.limit, max(1, args.batch))
    print(f"\nЗаписано векторов: {done}, осталось без вектора: {todo - done}.")
    return 0 if done or not todo else 1


if __name__ == "__main__":
    sys.exit(main())
