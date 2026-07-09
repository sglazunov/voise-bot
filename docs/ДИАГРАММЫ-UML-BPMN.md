# 📊 Диаграммы: UML и BPMN

Все диаграммы — на языке **Mermaid**, они отрисовываются прямо на GitHub. Здесь
собраны: варианты использования (Use Case), классы, последовательности,
активности, состояния, компоненты, развёртывание, а также бизнес-процессы в стиле
**BPMN**.

Смежные документы: **[Системный анализ](СИСТЕМНЫЙ-АНАЛИЗ.md)** (ER-диаграмма,
состояния), **[Бэкенд](БЕКЕНД.md)**, **[Архитектура](АРХИТЕКТУРА.md)**.

---

## 1. UML: Use Case (варианты использования)

Кто и что делает с системой.

```mermaid
flowchart LR
  U(["👤 Пользователь"])
  O(["👤 Организатор встреч"])
  A(["👤 Администратор"])

  subgraph S["Система linux-voise"]
    U1(("Загрузить и распознать файл"))
    U2(("Собрать протокол (ИИ)"))
    U3(("Определить говорящего"))
    U4(("Скачать TXT/SRT/JSON/Word"))
    U5(("Управлять ключами LLM"))
    U6(("Настроить автоматизацию"))
    U7(("Пометить встречи к записи"))
    U8(("Записать встречу сейчас"))
    U9(("Смотреть живые статусы"))
    U10(("Управлять доступом/юзерами"))
  end

  U --> U1 & U2 & U3 & U4 & U5 & U9
  O --> U6 & U7 & U8 & U9
  A --> U10 & U6
  U2 -. include .-> U1
  U3 -. extend .-> U1
```

---

## 2. UML: диаграмма классов (основные объекты бэкенда)

```mermaid
classDiagram
  class JobStore {
    +create(...) Job
    +get(id) Job
    +list(owner) Job[]
    -_worker_loop()
    -_cleaner_loop()
  }
  class Job {
    +id: str
    +owner: str
    +filename: str
    +status: str
    +progress: float
    +analyze: bool
    +diarize: bool
    +capture_screen: bool
    +identify_speakers: bool
    +provider: str
    +analysis: dict
  }
  class Segment {
    +start: float
    +end: float
    +text: str
    +speaker: str
  }
  class Scheduler {
    +status(user) dict
    +poll_now(user) dict
    +run_now(user, task_id)
    +stop_recording(...)
    -_loop()
    -_poll(user, cfg)
    -_maybe_trigger(user, cfg)
    -_passes_filter(st, cfg)
    -_run(st, slot)
  }
  class MeetingState {
    +key: str
    +title: str
    +url: str
    +start: datetime
    +state: str
    +record_flag: bool
    +job_id: str
  }
  class Meeting {
    +task_id
    +title: str
    +url: str
    +start: datetime
    +raw: dict
  }
  class LLMProvider {
    <<interface>>
    +complete(prompt, max_tokens) str
    +name: str
  }
  class RotatingProvider {
    +complete(...) str
    -keys: list
  }
  class WeeekClient {
    +upcoming_meetings(...) Meeting[]
    +custom_field_bool(task, name) bool
    +set_custom_field(...)
    +add_comment(...)
  }
  class CloudUploader {
    <<interface>>
    +upload(file, name, folder) dict
    +readiness() dict
  }
  class Recorder {
    +record_meeting(url, out, cfg) dict
    +acquire_slot() Slot
    +release_slot(slot)
  }

  JobStore "1" o-- "many" Job
  Job "1" *-- "many" Segment
  Scheduler "1" o-- "many" MeetingState
  Scheduler ..> JobStore : кладёт задачу
  Scheduler ..> WeeekClient : опрашивает
  Scheduler ..> Recorder : запускает запись
  Scheduler ..> CloudUploader : выгружает
  MeetingState ..> Job : порождает
  WeeekClient ..> Meeting : возвращает
  LLMProvider <|.. RotatingProvider
  Job ..> LLMProvider : протокол
```

---

## 3. UML: последовательность — ручное распознавание + протокол

```mermaid
sequenceDiagram
  actor U as Пользователь
  participant API as FastAPI (main.py)
  participant JS as JobStore
  participant WK as Воркер
  participant TR as transcribe.py
  participant SP as speaker_id.py
  participant AN as analyze.py
  participant LLM as LLM-провайдер
  participant DX as docx_export.py

  U->>API: POST /api/jobs (файл, опции)
  API->>JS: create(...) → job_id
  API-->>U: job_id
  loop опрос UI
    U->>API: GET /api/jobs/{id}/partial
    API-->>U: живой текст + %
  end
  JS->>WK: взять задачу (running)
  WK->>TR: transcribe_file(...)
  TR-->>WK: сегменты (start/end/text)
  opt identify_speakers
    WK->>SP: identify_speakers(video, segments)
    SP-->>WK: сегменты с именами
  end
  opt analyze=true
    WK->>AN: analyze_transcript(text)
    AN->>LLM: complete(prompt) [map-reduce]
    LLM-->>AN: JSON протокола
    AN-->>WK: структура протокола
    WK->>DX: generate_report → .docx
  end
  WK-->>API: status=done
  U->>API: GET /result?format=docx
  API-->>U: файл Word
```

---

## 4. UML: последовательность — автозапись встречи (без человека)

```mermaid
sequenceDiagram
  participant SCH as Scheduler (демон)
  participant WK as Weeek
  participant REC as Recorder (бот+ffmpeg)
  participant TM as Телемост
  participant CL as Облако
  participant JS as JobStore
  participant PIPE as Конвейер (ASR+протокол)

  loop каждые poll_interval
    SCH->>WK: upcoming_meetings()
    WK-->>SCH: встречи (ссылка+время)
  end
  Note over SCH: подошло время (− lookahead), фильтр пройден
  SCH->>REC: record_meeting(url) [слот]
  REC->>TM: зайти гостём (мик/камера выкл.)
  REC->>REC: ffmpeg пишет экран+звук
  Note over REC: стоп: все вышли / порог / потолок / вручную
  REC-->>SCH: файл записи
  SCH->>CL: upload(запись)
  SCH->>JS: create(analyze, identify_speakers, ocr)
  JS->>PIPE: распознать → кто говорил → протокол
  PIPE-->>SCH: .docx готов
  SCH->>CL: upload(протокол → отдельная папка)
  SCH->>WK: ссылки в поля + комментарий
```

---

## 5. UML: диаграмма активности — конвейер задачи

```mermaid
flowchart TD
  A([Старт задачи]) --> B[Распознать речь faster-whisper]
  B --> C{diarize?}
  C -- да --> C1[Диаризация по звуку]
  C -- нет --> D
  C1 --> D{identify_speakers?}
  D -- да --> D1[Имя говорящего с видео]
  D -- нет --> E
  D1 --> E[Записать TXT/SRT/JSON]
  E --> F{capture_screen?}
  F -- да --> F1[OCR текста с экрана]
  F -- нет --> G
  F1 --> G{analyze?}
  G -- нет --> Z([Готово])
  G -- да --> H[Собрать протокол map-reduce]
  H --> I[Сгенерировать .docx]
  I --> Z
  B -. ошибка .-> ERR([Ошибка])
  H -. отмена .-> CAN([Отменено])
```

---

## 6. UML: состояния (жизненные циклы)

Диаграммы состояний **Job** и **MeetingState** приведены в
**[Системном анализе, раздел 5](СИСТЕМНЫЙ-АНАЛИЗ.md#5-состояния-ключевых-объектов)**.
Кратко напоминание для встречи:

```mermaid
stateDiagram-v2
  [*] --> scheduled
  scheduled --> skipped : фильтр/галочка Weeek «нет»
  scheduled --> recording : пора писать
  recording --> uploading --> transcribing --> analyzing --> done
  scheduled --> missed
  done --> [*]
```

---

## 7. UML: компонентная диаграмма

```mermaid
flowchart TB
  subgraph Web["Веб-слой"]
    MAIN["FastAPI main.py<br/>+ шаблоны"]
  end
  subgraph CoreC["Ядро обработки"]
    JOBS["jobs.py"]; TRANS["transcribe.py"]; ANALYZE["analyze.py"]
    LLMM["llm.py"]; DOCXC["docx_export.py"]; SPK["speaker_id.py"]
    OCRC["screen_ocr.py"]; DIAC["diarize.py"]
  end
  subgraph AutoC["Автоматизация"]
    SCHED["scheduler.py"]; WEEEKC["weeek.py"]; SET["settings.py"]
    RECC["recorder/"]; CLC["clouds/"]
  end
  subgraph Sec["Безопасность"]
    SECC["security.py"]; UCRED["user_creds.py"]
  end
  MAIN --> JOBS & SCHED & SECC
  JOBS --> TRANS --> ANALYZE --> LLMM
  ANALYZE --> DOCXC
  JOBS --> SPK & OCRC & DIAC
  SCHED --> WEEEKC & RECC & CLC & SET
  SCHED --> JOBS
  LLMM --> UCRED
  MAIN --> SECC
```

---

## 8. UML: диаграмма развёртывания (deployment)

```mermaid
flowchart TB
  subgraph Server["Сервер Linux (24/7)"]
    subgraph Docker["Docker-контейнер linux-voise"]
      UV["uvicorn + FastAPI"]
      XVFB["Xvfb :99…:102<br/>(виртуальные экраны)"]
      PULSE["PulseAudio<br/>(meet0…meet3)"]
      CHROME["Chromium (Playwright)"]
      FFMPEG["ffmpeg (x11grab+pulse)"]
      TESS["Tesseract OCR"]
    end
    VOL[("Том /data<br/>(записи, задачи, настройки)")]
  end
  subgraph Ext["Внешние сервисы"]
    WEEEK["Weeek API"]; TM["Телемост"]; YD["Я.Диск / Google Drive"]
    OLL["Ollama (контейнер)"]; CLOUDLLM["Groq/Gemini/Yandex/GigaChat/Claude"]
  end
  Browser["Браузер пользователя"] -->|HTTPS| UV
  UV --- VOL
  CHROME --> TM
  FFMPEG --> VOL
  UV --> WEEEK & YD
  UV --> OLL & CLOUDLLM
  XVFB --- CHROME
  PULSE --- FFMPEG
```

---

## 9. BPMN: процесс «Автоматическая обработка встречи»

BPMN-нотация: **дорожки** (lanes) = участники процесса; прямоугольники = задачи;
ромбы = развилки (gateways); кружки = события начала/конца.

```mermaid
flowchart TB
  subgraph L1["Планировщик"]
    S((●)) --> P1["Опросить Weeek"]
    P1 --> G1{"Встреча со ссылкой<br/>и временем?"}
    G1 -- нет --> E1((✕))
    G1 -- да --> P2["Ждать момента старта<br/>(− lookahead)"]
    P2 --> G2{"Писать?<br/>ручной > Weeek > фильтры"}
    G2 -- нет --> E2["Пометить «не записываем»"] --> Efin((◉))
    G2 -- да --> P3["Занять слот"]
  end
  subgraph L2["Бот-рекордер"]
    P3 --> P4["Зайти в Телемост<br/>(гость, мик/камера выкл.)"]
    P4 --> P5["Писать экран+звук (ffmpeg)"]
    P5 --> G3{"Стоп?<br/>все вышли / порог / потолок / вручную"}
    G3 -- нет --> P5
    G3 -- да --> P6["Завершить запись"]
  end
  subgraph L3["Хранилище/Конвейер"]
    P6 --> P7["Выгрузить запись в облако"]
    P7 --> G4{"Распознавать?"}
    G4 -- нет --> P11["Только видео в облаке"] --> P10
    G4 -- да --> P8["Распознать речь + кто говорил"]
    P8 --> G5{"Протокол?"}
    G5 -- да --> P9["Собрать .docx + выгрузить"]
    G5 -- нет --> P10
    P9 --> P10["Ссылки в поля Weeek + комментарий"]
  end
  P10 --> Efin
```

---

## 10. BPMN: процесс «Ручное распознавание и протокол»

```mermaid
flowchart LR
  subgraph U["Пользователь"]
    A((●)) --> A1["Загрузить файл + выбрать опции"]
    A1 --> A2["Смотреть живой текст"]
    A4["Скачать результат"] --> Z((◉))
  end
  subgraph SYS["Система"]
    A1 --> B1["Поставить задачу в очередь"]
    B1 --> B2["Распознать речь"]
    B2 --> B3{"Опции: спикеры/OCR/протокол"}
    B3 --> B4["Применить включённые шаги"]
    B4 --> B5{"Протокол включён?"}
    B5 -- да --> B6["Собрать протокол → Word"]
    B5 -- нет --> B7["Готово (только текст)"]
    B6 --> A4
    B7 --> A4
  end
  A2 -. опрос статуса .- B2
```

---

## 11. BPMN: принятие решения «записывать встречу или нет»

```mermaid
flowchart TD
  ST((●)) --> D1{"Ручной выбор<br/>в интерфейсе?"}
  D1 -- "записывать" --> YES["✅ Записывать"]
  D1 -- "не записывать" --> NO["🚫 Не записывать"]
  D1 -- "не задан" --> D2{"Галочка Weeek<br/>«Запись встречи»?"}
  D2 -- "включена" --> YES
  D2 -- "выключена" --> NO
  D2 -- "поля нет" --> D3{"Режим по умолчанию?"}
  D3 -- "только выбранные" --> NO
  D3 -- "записывать все" --> D4{"Фильтры: слова /<br/>время / дни недели"}
  D4 -- "подходит" --> YES
  D4 -- "не подходит" --> NO
  YES --> EN((◉))
  NO --> EN
```

---

## 12. Как поддерживать диаграммы в актуальности

- Диаграммы — **текст** (Mermaid), поэтому правятся как код и версионируются в git.
- При изменении процесса/классов правьте соответствующий блок здесь и, если нужно,
  в **[Архитектуре](АРХИТЕКТУРА.md)** (там — потоки данных верхнего уровня).
- Источник правды — код в `app/`; диаграммы должны ему соответствовать.
