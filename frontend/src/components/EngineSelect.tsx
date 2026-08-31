import { useMemo } from "react";
import { Select } from "./ui";

export type Engine = {
  value: string;            // «custom:gpt://b1g/deepseek…», «nvidia:модель», «gemini»
  label: string;
  group?: string;           // человеческое имя поставщика
  model?: string;           // имя модели без поставщика
};

/* Выбор движка ДВУМЯ полями: сначала поставщик, потом его модель.
   Раньше это был один список, и когда подключается несколько ключей, он
   вырастает до сотни строк вида «Свой ключ · …» — из которых не видно, где
   OpenRouter, где Yandex Cloud, а где свой сервер. Выбрать нужную модель в
   таком списке невозможно, а ошибка обнаруживается уже на собранном протоколе. */
export function EngineSelect({ engines, value, onChange, allowAuto = true }: {
  engines: Engine[];
  value: string;
  onChange: (v: string) => void;
  allowAuto?: boolean;
}) {
  const groups = useMemo(() => {
    const out: string[] = [];
    for (const e of engines) {
      const g = e.group || "Прочее";
      if (!out.includes(g)) out.push(g);
    }
    return out;
  }, [engines]);

  // Поставщик текущего выбора. Для «авто» — пустая строка: моделей у него нет.
  const current = engines.find((e) => e.value === value);
  const group = current?.group || "";
  const mine = engines.filter((e) => (e.group || "Прочее") === group);

  function pickGroup(g: string) {
    if (g === "auto") { onChange("auto"); return; }
    // При смене поставщика берём его первую модель: иначе остались бы с
    // выбором от прежнего, и он молча не совпадал бы с показанным поставщиком.
    const first = engines.find((e) => (e.group || "Прочее") === g);
    if (first) onChange(first.value);
  }

  // Выбор указывает на модель, которой в списке уже нет: поставщик её убрал
  // (так у NVIDIA пропал DeepSeek) или ключ отключили. Молчать нельзя —
  // протокол соберётся не тем движком, и человек узнает об этом из шапки.
  const lost = value && value !== "auto" && !current;

  return (
    <>
    {lost && (
      <div className="text-[11.5px] mb-1" style={{ color: "#fbbf24" }}>
        Выбранная модель «{value}» сейчас недоступна — её убрал поставщик или
        отключён ключ. Выберите другую, иначе протокол соберёт запасной движок.
      </div>
    )}
    <div className="grid md:grid-cols-2 gap-2">
      <Select
        value={value === "auto" || !value ? "auto" : group}
        onChange={pickGroup}
        options={[
          ...(allowAuto ? [{ value: "auto", label: "Авто — первый настроенный" }] : []),
          ...groups.map((g) => ({ value: g, label: g })),
        ]}
      />
      {value !== "auto" && value !== "" && mine.length > 0 && (
        <Select
          value={value}
          onChange={onChange}
          options={mine.map((e) => ({ value: e.value, label: e.model || e.label }))}
        />
      )}
    </div>
    </>
  );
}
