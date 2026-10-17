(function () {
  "use strict";

  // Ограничения из CONTRACTS.md
  const MAX_UPLOAD_BYTES = 100 * 1024 * 1024; // 100 MiB
  const POLL_INTERVAL_MS = 2000;

  // Синтетический JPEG из приложенного tests/fixtures/contracts/done_violation.json
  const SYNTHETIC_JPEG_BASE64 =
    "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";

  // Синтетические фикстуры строго по схеме CONTRACTS.md
  const SYNTHETIC_FIXTURES = {
    accepted: {
      job_id: "synthetic-job"
    },
    processing: {
      job_id: "synthetic-job",
      status: "processing"
    },
    failed: {
      job_id: "synthetic-job",
      status: "failed"
    },
    no_violation: {
      job_id: "synthetic-job",
      status: "done",
      verdict: "no_violation",
      violations: []
    },
    violation: {
      job_id: "synthetic-job",
      status: "done",
      verdict: "violation",
      violations: [
        {
          type: "synthetic_contract_example",
          timecode_sec: 1.0,
          description:
            "СИНТЕТИЧЕСКИЙ ТЕСТ КОНТРАКТА. Это не наблюдение и не нормативное заключение. Тестовый источник: https://example.invalid/synthetic-rule (не действующий акт).",
          evidence_frames: [
            {
              timestamp_sec: 1.0,
              image_base64: SYNTHETIC_JPEG_BASE64
            }
          ]
        },
        {
          type: "synthetic_contract_example",
          timecode_sec: 3.0,
          description:
            "СИНТЕТИЧЕСКИЙ ТЕСТ КОНТРАКТА. Это не наблюдение и не нормативное заключение. Тестовый источник: https://example.invalid/synthetic-rule (не действующий акт).",
          evidence_frames: [
            {
              timestamp_sec: 3.0,
              image_base64: SYNTHETIC_JPEG_BASE64
            },
            {
              timestamp_sec: 3.1,
              image_base64: SYNTHETIC_JPEG_BASE64
            },
            {
              timestamp_sec: 3.2,
              image_base64: SYNTHETIC_JPEG_BASE64
            }
          ]
        }
      ]
    }
  };

  // Оперативное состояние в памяти вкладки (без localStorage/sessionStorage/cookie)
  const state = {
    apiKey: "",
    demoMode: false,
    selectedFile: null,
    activeJobId: null,
    pollTimerId: null,
    elapsedTimerId: null,
    startedAtMs: null,
    demoPollTicks: 0,
    run: null,
    videoUrl: null,
    videoReady: false
  };

  // Ссылки на DOM-элементы
  const dom = {
    demoBanner: document.getElementById("demoBanner"),
    notice: document.getElementById("notice"),
    videoPanel: document.getElementById("videoPanel"),
    videoHome: document.getElementById("videoHome"),
    resultVideoSlot: document.getElementById("resultVideoSlot"),
    video: document.getElementById("localVideo"),
    videoMessage: document.getElementById("videoMessage"),
    processingHeading: document.getElementById("processingHeading"),
    processingDescription: document.getElementById("processingDescription"),
    demoModeToggle: document.getElementById("demoModeToggle"),
    demoSettingsBox: document.getElementById("demoSettingsBox"),
    demoScenarioSelect: document.getElementById("demoScenarioSelect"),

    apiKeyInput: document.getElementById("apiKeyInput"),
    keyStatusBadge: document.getElementById("keyStatusBadge"),
    toggleKeyVisibilityBtn: document.getElementById("toggleKeyVisibilityBtn"),
    clearKeyBtn: document.getElementById("clearKeyBtn"),

    errorAlert: document.getElementById("errorAlert"),
    errorTitle: document.getElementById("errorTitle"),
    errorCodeBadge: document.getElementById("errorCodeBadge"),
    errorMessage: document.getElementById("errorMessage"),
    errorResetBtn: document.getElementById("errorResetBtn"),

    uploadSection: document.getElementById("uploadSection"),
    dropZone: document.getElementById("dropZone"),
    fileInput: document.getElementById("fileInput"),
    selectedFilePanel: document.getElementById("selectedFilePanel"),
    selectedFileName: document.getElementById("selectedFileName"),
    selectedFileSize: document.getElementById("selectedFileSize"),
    changeFileBtn: document.getElementById("changeFileBtn"),
    startAnalysisBtn: document.getElementById("startAnalysisBtn"),

    processingSection: document.getElementById("processingSection"),
    processingJobId: document.getElementById("processingJobId"),
    processingFileName: document.getElementById("processingFileName"),
    processingElapsed: document.getElementById("processingElapsed"),
    cancelPollingBtn: document.getElementById("cancelPollingBtn"),

    resultSection: document.getElementById("resultSection"),
    verdictBanner: document.getElementById("verdictBanner"),
    resultHeading: document.getElementById("resultHeading"),
    verdictSubtitle: document.getElementById("verdictSubtitle"),
    verdictRawBadge: document.getElementById("verdictRawBadge"),
    newUploadBtn: document.getElementById("newUploadBtn"),
    resultJobId: document.getElementById("resultJobId"),
    resultStatus: document.getElementById("resultStatus"),
    resultFileName: document.getElementById("resultFileName"),
    resultEventsCount: document.getElementById("resultEventsCount"),
    violationsContainer: document.getElementById("violationsContainer"),
    violationsList: document.getElementById("violationsList"),

    imageModal: document.getElementById("imageModal"),
    modalBackdrop: document.getElementById("modalBackdrop"),
    closeModalBtn: document.getElementById("closeModalBtn"),
    modalTitle: document.getElementById("modalTitle"),
    modalMeta: document.getElementById("modalMeta"),
    modalImage: document.getElementById("modalImage")
  };

  // Форматирование секунд в MM:SS.s (плюс точное значение из контракта)
  function formatTimecode(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
      return "Некорректный таймкод";
    }
    const mins = Math.floor(seconds / 60);
    const remSec = seconds - mins * 60;
    const mm = String(mins).padStart(2, "0");
    const ss = remSec.toFixed(1).padStart(4, "0");
    return `${mm}:${ss} (${Number(seconds.toFixed(3))} с)`;
  }

  function formatElapsed(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }

  function formatFileSize(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КиБ`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} МиБ`;
  }

  // Безопасная отрисовка описания с выделением HTTP(S) ссылок из текста description
  function renderDescriptionWithLinks(containerEl, text) {
    containerEl.textContent = "";
    if (typeof text !== "string" || !text) {
      return;
    }
    const urlRegex = /(https?:\/\/[^\s)<>"]+)/g;
    let lastIndex = 0;
    let match;

    while ((match = urlRegex.exec(text)) !== null) {
      if (match.index > lastIndex) {
        containerEl.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
      }
      const rawUrl = match[0];
      const link = document.createElement("a");
      link.href = rawUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = rawUrl;
      containerEl.appendChild(link);
      lastIndex = match.index + rawUrl.length;
    }

    if (lastIndex < text.length) {
      containerEl.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
  }

  // Reject malformed results before rendering any verdict or evidence.
  function exactObject(value, keys) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Object.keys(value).length !== keys.length ||
        !keys.every(key => Object.hasOwn(value, key))) {
      throw new Error("Ответ сервера имеет неожиданную структуру. Результат не показан.");
    }
  }

  function nonEmpty(value) {
    return typeof value === "string" && value.trim().length > 0;
  }

  function validTime(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
  }

  function validateJpeg(encoded) {
    if (typeof encoded !== "string" || !encoded.length || encoded.length > 6990508 ||
        encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) {
      throw new Error("Некорректное изображение в результате. Ожидается JPEG.");
    }
    const bytes = atob(encoded);
    if (btoa(bytes) !== encoded || bytes.length > 5 * 1024 * 1024 ||
        bytes.charCodeAt(0) !== 255 || bytes.charCodeAt(1) !== 216 ||
        bytes.charCodeAt(bytes.length - 2) !== 255 || bytes.charCodeAt(bytes.length - 1) !== 217) {
      throw new Error("Некорректное изображение в результате. Ожидается JPEG.");
    }
  }

  function validateJobResponsePayload(data, jobId) {
    exactObject(data, data && data.status === "done"
      ? ["job_id", "status", "verdict", "violations"] : ["job_id", "status"]);
    if (!nonEmpty(data.job_id) || data.job_id !== jobId) {
      throw new Error("Ответ относится к другому заданию. Результат не показан.");
    }
    if (data.status === "processing" || data.status === "failed") return;
    if (data.status !== "done" || !["violation", "no_violation"].includes(data.verdict) ||
        !Array.isArray(data.violations) ||
        (data.verdict === "violation") !== (data.violations.length > 0)) {
      throw new Error("Некорректный итог анализа. Результат не показан.");
    }
    let previous = -1;
    const withinVideo = time => validTime(time) &&
      (!state.videoReady || time <= dom.video.duration);
    data.violations.forEach(event => {
      exactObject(event, ["type", "timecode_sec", "description", "evidence_frames"]);
      if (!nonEmpty(event.type) || !nonEmpty(event.description) ||
          !withinVideo(event.timecode_sec) || event.timecode_sec < previous ||
          !Array.isArray(event.evidence_frames) || event.evidence_frames.length < 1 ||
          event.evidence_frames.length > 3) {
        throw new Error("Некорректные поля события или таймкод. Результат не показан.");
      }
      previous = event.timecode_sec;
      event.evidence_frames.forEach(frame => {
        exactObject(frame, ["timestamp_sec", "image_base64"]);
        if (!withinVideo(frame.timestamp_sec)) {
          throw new Error("Некорректный таймкод кадра. Результат не показан.");
        }
        validateJpeg(frame.image_base64);
      });
    });
  }

  async function validateEvidenceImages(data, run) {
    if (data.status !== "done") return;
    // Decode before showing a verdict; signatures alone do not prove a valid JPEG.
    for (const event of data.violations) {
      for (const frame of event.evidence_frames) {
        if (!isCurrent(run)) return;
        const image = new Image();
        image.src = `data:image/jpeg;base64,${frame.image_base64}`;
        await image.decode();
        if (!image.naturalWidth || image.naturalWidth * image.naturalHeight > 16000000) {
          throw new Error("Изображение кадра повреждено или слишком велико.");
        }
      }
    }
  }

  // Управление API-ключом в памяти
  function updateApiKeyState() {
    state.apiKey = dom.apiKeyInput.value.trim();
    if (state.apiKey.length > 0) {
      dom.keyStatusBadge.textContent = "Ключ в памяти";
      dom.keyStatusBadge.className = "badge badge--ok";
    } else {
      dom.keyStatusBadge.textContent = "Ключ не задан";
      dom.keyStatusBadge.className = "badge badge--neutral";
    }
  }

  // Переключение деморежима
  function setDemoMode(enabled) {
    const interrupted = state.run && !state.run.demoMode;
    resetToUploadScreen(true);
    state.demoMode = Boolean(enabled);
    dom.demoModeToggle.checked = state.demoMode;
    dom.demoBanner.classList.toggle("hidden", !state.demoMode);
    dom.demoSettingsBox.classList.toggle("hidden", !state.demoMode);
    hideError();
    if (interrupted) showStoppedNotice();
  }

  // Отображение ошибок
  function showError(title, message, codeText) {
    dom.errorTitle.textContent = title;
    dom.errorMessage.textContent = message;
    if (codeText) {
      dom.errorCodeBadge.textContent = codeText;
      dom.errorCodeBadge.classList.remove("hidden");
    } else {
      dom.errorCodeBadge.textContent = "";
      dom.errorCodeBadge.classList.add("hidden");
    }
    dom.errorAlert.classList.remove("hidden");
  }

  function hideError() {
    dom.errorAlert.classList.add("hidden");
  }

  function mapHttpError(status) {
    switch (status) {
      case 401:
        return {
          title: "Ошибка авторизации (HTTP 401)",
          message: "Передан неверный или отсутствующий заголовок X-API-Key. Проверьте введённый API-ключ и повторите попытку.",
          code: "HTTP 401"
        };
      case 404:
        return {
          title: "Задание не найдено (HTTP 404)",
          message: "Запрошенный идентификатор job_id не существует на сервере.",
          code: "HTTP 404"
        };
      case 413:
        return {
          title: "Превышен допустимый размер файла (HTTP 413)",
          message: "Размер загружаемого видеофайла превышает установленный лимит сервера (100 МиБ).",
          code: "HTTP 413"
        };
      case 415:
        return {
          title: "Неподдерживаемый формат файла (HTTP 415)",
          message: "Сервер принимает только видеофайлы в контейнере QuickTime (.mov).",
          code: "HTTP 415"
        };
      case 422:
        return {
          title: "Файл повреждён или некорректен (HTTP 422)",
          message: "Сервер не смог прочитать контейнер файла до постановки задачи в очередь. Проверьте целостность .mov ролика.",
          code: "HTTP 422"
        };
      case 503:
        return {
          title: "Сервер анализа занят или недоступен (HTTP 503)",
          message: "Сервис занят другим заданием или ещё готовится к работе. Повторите отправку позже.",
          code: "HTTP 503"
        };
      default:
        return {
          title: `Ошибка обращения к API (HTTP ${status})`,
          message: "Сервер вернул неожиданный код ответа при выполнении запроса.",
          code: `HTTP ${status}`
        };
    }
  }

  // Выбор и валидация файла .mov
  function handleFileSelection(file) {
    resetToUploadScreen(false);
    hideError();
    if (!file) return;

    const fileName = file.name || "";
    const isMovExtension = fileName.toLowerCase().endsWith(".mov");

    if (!isMovExtension) {
      showError(
        "Неподдерживаемое расширение файла",
        "Согласно контракту сервиса assar, к загрузке допускаются только видеофайлы формата .mov.",
        "Клиентская проверка"
      );
      dom.fileInput.value = "";
      return;
    }

    if (file.size === 0) {
      showError(
        "Выбран пустой файл",
        "Размер выбранного файла равен 0 байт. Выберите корректную видеозапись.",
        "Клиентская проверка"
      );
      dom.fileInput.value = "";
      return;
    }

    if (file.size > MAX_UPLOAD_BYTES) {
      showError(
        "Превышен лимит размера файла",
        `Размер файла (${formatFileSize(file.size)}) превышает максимальный лимит 100 МиБ.`,
        "Клиентская проверка"
      );
      dom.fileInput.value = "";
      return;
    }

    state.selectedFile = file;
    dom.selectedFileName.textContent = file.name;
    dom.selectedFileSize.textContent = `(${formatFileSize(file.size)})`;
    dom.selectedFilePanel.classList.remove("hidden");
    state.videoUrl = URL.createObjectURL(file);
    dom.video.src = state.videoUrl;
    dom.videoPanel.classList.remove("hidden");
    dom.videoMessage.textContent = "Загрузка локального предпросмотра…";
  }

  function showStoppedNotice() {
    dom.notice.textContent = "Ожидание остановлено только в браузере. Если сервер уже принял видео, обработка продолжится. Новая загрузка может быть временно недоступна, пока сервис занят.";
    dom.notice.classList.remove("hidden");
  }

  function invalidateRun() {
    stopPollingTimers();
    if (state.run) state.run.controller.abort();
    state.run = null;
    state.activeJobId = null;
    dom.demoScenarioSelect.disabled = false;
  }

  function isCurrent(run) {
    return state.run === run && !run.controller.signal.aborted;
  }

  function releaseVideo() {
    state.videoReady = false;
    dom.video.pause();
    dom.video.removeAttribute("src");
    dom.video.load();
    if (state.videoUrl) URL.revokeObjectURL(state.videoUrl);
    state.videoUrl = null;
    dom.videoPanel.classList.add("hidden");
  }

  function seekVideo(seconds) {
    if (!state.videoReady || dom.video.error) {
      dom.videoMessage.textContent = "Переход недоступен: дождитесь загрузки видео или откройте исходный файл в локальном проигрывателе.";
      return;
    }
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > dom.video.duration) {
      dom.videoMessage.textContent = "Таймкод выходит за пределы выбранного видео. Переход не выполнен.";
      return;
    }
    dom.video.currentTime = seconds;
    dom.videoMessage.textContent = `Переход к ${formatTimecode(seconds)}. Нажмите «Воспроизвести» в плеере.`;
    dom.video.scrollIntoView({ block: "center" });
  }

  function timecodeButton(seconds, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--secondary btn--sm mono";
    button.textContent = `${label}${formatTimecode(seconds)}`;
    button.addEventListener("click", function () { seekVideo(seconds); });
    return button;
  }

  // Остановка таймеров опроса
  function stopPollingTimers() {
    if (state.pollTimerId !== null) {
      clearTimeout(state.pollTimerId);
      state.pollTimerId = null;
    }
    if (state.elapsedTimerId !== null) {
      clearInterval(state.elapsedTimerId);
      state.elapsedTimerId = null;
    }
  }

  // Сброс к начальному экрану выбора файла
  function resetToUploadScreen(keepSelectedFile) {
    dom.videoHome.append(dom.videoPanel);
    invalidateRun();
    closeImageModal();
    dom.violationsList.textContent = "";
    dom.notice.classList.add("hidden");
    state.activeJobId = null;
    state.demoPollTicks = 0;

    dom.processingSection.classList.add("hidden");
    dom.resultSection.classList.add("hidden");
    dom.uploadSection.classList.remove("hidden");
    dom.startAnalysisBtn.disabled = false;

    if (!keepSelectedFile) {
      releaseVideo();
      state.selectedFile = null;
      dom.fileInput.value = "";
      dom.selectedFileName.textContent = "";
      dom.selectedFileSize.textContent = "";
      dom.selectedFilePanel.classList.add("hidden");
    }
  }

  // Переход в экран ожидания обработки
  function enterProcessingScreen(jobId, fileName) {
    hideError();
    state.activeJobId = jobId;
    state.startedAtMs = state.run.startedAtMs;

    dom.uploadSection.classList.add("hidden");
    dom.resultSection.classList.add("hidden");
    dom.processingSection.classList.remove("hidden");

    dom.processingJobId.textContent = jobId || "Ожидаем подтверждения приёма";
    dom.processingHeading.textContent = jobId ? "Выполняется анализ видеозаписи" : "Отправка видеозаписи";
    dom.processingDescription.textContent = state.run.demoMode
      ? "Демонстрация на синтетических данных. Видео не отправляется на сервер."
      : jobId ? "Видео принято. Ожидаем результат анализа; процент готовности недоступен."
        : "Передаём видео сервису. Приём задания ещё не подтверждён.";
    dom.processingFileName.textContent = fileName || "—";
    dom.processingElapsed.textContent = "00:00";

    if (state.elapsedTimerId !== null) {
      clearInterval(state.elapsedTimerId);
    }
    state.elapsedTimerId = setInterval(function () {
      const elapsed = Date.now() - state.startedAtMs;
      dom.processingElapsed.textContent = formatElapsed(elapsed);
    }, 500);
  }

  // Отрисовка результата done (violation / no_violation)
  function renderDoneResult(jobData) {
    dom.resultVideoSlot.append(dom.videoPanel);
    invalidateRun();
    hideError();

    dom.uploadSection.classList.add("hidden");
    dom.processingSection.classList.add("hidden");
    dom.resultSection.classList.remove("hidden");

    const isViolation = jobData.verdict === "violation";
    const violations = Array.isArray(jobData.violations) ? jobData.violations : [];

    dom.verdictBanner.className =
      "card verdict-card " + (isViolation ? "verdict-card--violation" : "verdict-card--no-violation");

    if (isViolation) {
      dom.resultHeading.textContent = "Обнаружены признаки нарушения";
      dom.verdictSubtitle.textContent =
        "По результатам визуального анализа видеозаписи зафиксированы события, требующие внимания проверяющего.";
      dom.verdictRawBadge.className = "badge badge--error mono";
      dom.verdictRawBadge.textContent = "verdict: violation";
    } else {
      dom.resultHeading.textContent = "Нарушений не выявлено";
      dom.verdictSubtitle.textContent =
        "На проверенной видеозаписи подтверждённых событий из каталога правил не зафиксировано.";
      dom.verdictRawBadge.className = "badge badge--ok mono";
      dom.verdictRawBadge.textContent = "verdict: no_violation";
    }

    dom.resultJobId.textContent = jobData.job_id;
    dom.resultStatus.textContent = jobData.status;
    dom.resultFileName.textContent = state.selectedFile ? state.selectedFile.name : "—";
    dom.resultEventsCount.textContent = String(violations.length);

    // Сортировка событий по timecode_sec согласно CONTRACTS.md
    const sortedViolations = violations.slice().sort(function (a, b) {
      return a.timecode_sec - b.timecode_sec;
    });

    dom.violationsList.textContent = "";

    if (sortedViolations.length > 0) {
      dom.violationsContainer.classList.remove("hidden");
      sortedViolations.forEach(function (ev, idx) {
        const card = createEventCardElement(ev, idx + 1);
        dom.violationsList.appendChild(card);
      });
    } else {
      dom.violationsContainer.classList.add("hidden");
    }
  }

  function createEventCardElement(eventItem, indexNumber) {
    const article = document.createElement("article");
    article.className = "event-card";

    // Верхняя строка карточки события
    const topRow = document.createElement("div");
    topRow.className = "event-card__top";

    const titleGroup = document.createElement("div");
    titleGroup.className = "event-card__title-group";

    const indexBadge = document.createElement("span");
    indexBadge.className = "event-card__index";
    indexBadge.textContent = `Событие #${indexNumber}`;

    const typeSpan = document.createElement("span");
    typeSpan.className = "event-card__type";
    typeSpan.textContent = eventItem.type;

    titleGroup.appendChild(indexBadge);
    titleGroup.appendChild(typeSpan);

    const timecodeSpan = timecodeButton(eventItem.timecode_sec, "Перейти к ");

    topRow.appendChild(titleGroup);
    topRow.appendChild(timecodeSpan);
    article.appendChild(topRow);

    // Описание события (с наблюдением и нормативной ссылкой внутри description)
    const descParagraph = document.createElement("p");
    descParagraph.className = "event-card__description";
    renderDescriptionWithLinks(descParagraph, eventItem.description || "");
    article.appendChild(descParagraph);

    // Сетка кадров-доказательств (1–3 кадра)
    const frames = Array.isArray(eventItem.evidence_frames) ? eventItem.evidence_frames : [];
    if (frames.length > 0) {
      const grid = document.createElement("div");
      grid.className = "evidence-grid";

      frames.forEach(function (frame, frameIdx) {
        const figure = document.createElement("figure");
        figure.className = "frame-card";

        const dataUri = `data:image/jpeg;base64,${frame.image_base64}`;

        const imgBtn = document.createElement("button");
        imgBtn.type = "button";
        imgBtn.className = "frame-card__btn";
        imgBtn.setAttribute(
          "aria-label",
          `Открыть кадр ${frameIdx + 1} события ${indexNumber} на отметке ${formatTimecode(frame.timestamp_sec)}`
        );

        const img = document.createElement("img");
        img.className = "frame-card__img";
        img.src = dataUri;
        img.alt = `Кадр-доказательство ${frameIdx + 1} (${eventItem.type})`;
        img.loading = "lazy";

        imgBtn.appendChild(img);
        imgBtn.addEventListener("click", function () {
          openImageModal(
            dataUri,
            `Событие #${indexNumber}: ${eventItem.type}`,
            `Кадр ${frameIdx + 1} из ${frames.length} • PTS: ${formatTimecode(frame.timestamp_sec)}`
          );
        });

        const caption = document.createElement("figcaption");
        caption.className = "frame-card__caption";

        const frameLabel = document.createElement("span");
        frameLabel.textContent = `Кадр #${frameIdx + 1}`;

        const tsLabel = timecodeButton(frame.timestamp_sec, "Перейти к ");

        caption.appendChild(frameLabel);
        caption.appendChild(tsLabel);

        figure.appendChild(imgBtn);
        figure.appendChild(caption);
        grid.appendChild(figure);
      });

      article.appendChild(grid);
    }

    return article;
  }

  // Each operation owns its immutable mode/key/file and an abort signal.
  async function startAnalysis() {
    if (state.run) return;
    hideError();
    updateApiKeyState();
    if (!state.selectedFile) {
      showError("Файл не выбран", "Выберите видеофайл формата .mov перед запуском проверки.", null);
      return;
    }
    if (!state.demoMode && !state.apiKey) {
      showError("Не указан API-ключ", "Введите ключ доступа к сервису.", null);
      dom.apiKeyInput.focus();
      return;
    }
    const run = {
      controller: new AbortController(),
      demoMode: state.demoMode,
      scenario: dom.demoScenarioSelect.value,
      file: state.selectedFile,
      apiKey: state.apiKey,
      startedAtMs: Date.now(),
      jobId: null,
      demoTicks: 0
    };
    state.run = run;
    dom.notice.classList.add("hidden");
    dom.startAnalysisBtn.disabled = true;
    dom.demoScenarioSelect.disabled = true;
    enterProcessingScreen(null, run.file.name);
    if (run.demoMode) {
      if (run.scenario.startsWith("http_")) {
        failHttp(run, Number(run.scenario.slice(5)));
        return;
      }
      run.jobId = SYNTHETIC_FIXTURES.accepted.job_id;
      enterProcessingScreen(run.jobId, run.file.name);
      scheduleNextPoll(run);
      return;
    }
    try {
      const formData = new FormData();
      formData.append("file", run.file);
      const response = await fetch("/api/v1/analyze", {
        method: "POST", headers: { "X-API-Key": run.apiKey }, body: formData,
        signal: run.controller.signal, redirect: "error"
      });
      if (!isCurrent(run)) return;
      if (!response.ok) { failHttp(run, response.status); return; }
      const data = await response.json();
      if (!isCurrent(run)) return;
      exactObject(data, ["job_id"]);
      if (!nonEmpty(data.job_id)) throw new Error("invalid acceptance");
      run.jobId = data.job_id;
      enterProcessingScreen(run.jobId, run.file.name);
      scheduleNextPoll(run);
    } catch (error) {
      if (!isCurrent(run)) return;
      resetToUploadScreen(true);
      showError("Не удалось отправить видео", "Проверьте соединение и доступность сервиса. Корректное подтверждение приёма не получено.", null);
      showStoppedNotice();
    }
  }

  function failHttp(run, status) {
    if (!isCurrent(run)) return;
    const info = mapHttpError(status);
    resetToUploadScreen(true);
    showError(info.title, info.message + (run.demoMode ? " (Синтетическая демонстрация)" : ""), info.code);
  }

  function scheduleNextPoll(run) {
    if (!isCurrent(run)) return;
    clearTimeout(state.pollTimerId);
    state.pollTimerId = setTimeout(() => pollJobStatus(run), POLL_INTERVAL_MS);
  }

  async function pollJobStatus(run) {
    if (!isCurrent(run)) return;
    if (run.demoMode) {
      run.demoTicks += 1;
      if (run.demoTicks < 2) { scheduleNextPoll(run); return; }
      await handleJobResponseData(SYNTHETIC_FIXTURES[run.scenario], run);
      return;
    }
    try {
      const response = await fetch(`/api/v1/jobs/${encodeURIComponent(run.jobId)}`, {
        headers: { "X-API-Key": run.apiKey }, signal: run.controller.signal, redirect: "error"
      });
      if (!isCurrent(run)) return;
      if (!response.ok) { failHttp(run, response.status); return; }
      const data = await response.json();
      if (!isCurrent(run)) return;
      await handleJobResponseData(data, run);
    } catch (error) {
      if (!isCurrent(run)) return;
      resetToUploadScreen(true);
      showError("Не удалось получить результат", "Соединение прервано или ответ сервера не удалось прочитать. Итог анализа неизвестен.", null);
      showStoppedNotice();
    }
  }

  async function handleJobResponseData(data, run) {
    if (!isCurrent(run)) return;
    try {
      validateJobResponsePayload(data, run.jobId);
      await validateEvidenceImages(data, run);
    } catch (error) {
      if (!isCurrent(run)) return;
      resetToUploadScreen(true);
      showError("Некорректный результат сервера", "События, таймкоды или изображения не прошли проверку. Итог анализа не показан; повторите попытку или обратитесь к команде сервиса.", null);
      return;
    }
    if (!isCurrent(run)) return;
    if (data.status === "processing") { scheduleNextPoll(run); return; }
    if (data.status === "failed") {
      resetToUploadScreen(true);
      showError("Не удалось выполнить анализ", "Сервис сообщил об ошибке обработки видео. Это не означает отсутствие нарушений. Проверьте файл и повторите попытку.", "failed");
      return;
    }
    renderDoneResult(data);
  }

  // Модальное окно для просмотра кадров
  function openImageModal(src, title, metaText) {
    dom.modalImage.src = src;
    dom.modalTitle.textContent = title;
    dom.modalMeta.textContent = metaText;
    dom.imageModal.classList.remove("hidden");
  }

  function closeImageModal() {
    dom.imageModal.classList.add("hidden");
    dom.modalImage.removeAttribute("src");
  }

  // Привязка обработчиков событий
  function initEventListeners() {
    dom.apiKeyInput.addEventListener("input", updateApiKeyState);

    dom.toggleKeyVisibilityBtn.addEventListener("click", function () {
      const isPassword = dom.apiKeyInput.type === "password";
      dom.apiKeyInput.type = isPassword ? "text" : "password";
      dom.toggleKeyVisibilityBtn.textContent = isPassword ? "Скрыть" : "Показать";
    });

    dom.clearKeyBtn.addEventListener("click", function () {
      dom.apiKeyInput.value = "";
      updateApiKeyState();
      dom.apiKeyInput.focus();
    });

    dom.demoModeToggle.addEventListener("change", function (e) {
      setDemoMode(e.target.checked);
    });

    // Выбор файла через клик и Drag-and-Drop
    dom.dropZone.addEventListener("click", function (e) {
      if (e.target !== dom.fileInput) dom.fileInput.click();
    });

    dom.dropZone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        dom.fileInput.click();
      }
    });

    dom.fileInput.addEventListener("change", function (e) {
      handleFileSelection(e.target.files && e.target.files[0]);
    });

    dom.dropZone.addEventListener("dragover", function (e) {
      e.preventDefault();
      dom.dropZone.classList.add("dropzone--active");
    });

    dom.dropZone.addEventListener("dragleave", function () {
      dom.dropZone.classList.remove("dropzone--active");
    });

    dom.dropZone.addEventListener("drop", function (e) {
      e.preventDefault();
      dom.dropZone.classList.remove("dropzone--active");
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        handleFileSelection(e.dataTransfer.files[0]);
      }
    });

    dom.changeFileBtn.addEventListener("click", function () {
      resetToUploadScreen(false);
      dom.fileInput.click();
    });

    dom.startAnalysisBtn.addEventListener("click", startAnalysis);

    dom.cancelPollingBtn.addEventListener("click", function () {
      const realRun = state.run && !state.run.demoMode;
      resetToUploadScreen(false);
      if (realRun) showStoppedNotice();
    });

    dom.newUploadBtn.addEventListener("click", function () {
      resetToUploadScreen(false);
    });

    dom.errorResetBtn.addEventListener("click", function () {
      hideError();
      resetToUploadScreen(false);
    });

    dom.closeModalBtn.addEventListener("click", closeImageModal);
    dom.modalBackdrop.addEventListener("click", closeImageModal);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !dom.imageModal.classList.contains("hidden")) {
        closeImageModal();
      }
    });
  }

  dom.video.addEventListener("loadedmetadata", function () {
    state.videoReady = Number.isFinite(dom.video.duration) && dom.video.duration > 0;
    dom.videoMessage.textContent = "Локальный предпросмотр. Таймкоды событий и кадров позволяют перейти к нужному моменту. Звук выключен.";
  });
  dom.video.addEventListener("error", function () {
    if (!state.videoUrl) return;
    state.videoReady = false;
    dom.videoMessage.textContent = "Браузер не может воспроизвести это видео: кодек не поддерживается или файл повреждён. Откройте исходный файл в локальном проигрывателе. Анализ на сервере остаётся доступен.";
  });
  window.addEventListener("pagehide", function () {
    resetToUploadScreen(false);
    dom.apiKeyInput.value = "";
    updateApiKeyState();
  });
  initEventListeners();
  // Browsers can restore form controls on reload: always start with the real API.
  setDemoMode(false);
  dom.apiKeyInput.value = "";
  updateApiKeyState();
})();
