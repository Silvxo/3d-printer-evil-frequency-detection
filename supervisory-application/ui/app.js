/**
 * Supervisory Application UI Controller (`window.SupervisoryUI`)
 * Manages tab navigation, Data Capture recording session state (synced with backend HTTP API),
 * Data Analysis CSV file selection dropdown, and per-axis (X, Y, Z) canvas rendering
 * for Full Frequency Spectrum and FFT Ghost Frequencies.
 */
(function () {
  "use strict";

  if (typeof window === "undefined" || typeof document === "undefined") {
    console.log(
      "[SupervisoryUI] Browser script detected running in Node.js. " +
        "Please start the full application with ./start.sh and open http://127.0.0.1:8000 in your browser."
    );
    return;
  }

  const STATE = {
    IDLE: "Idle",
    RECORDING: "Recording",
    PAUSED: "Paused",
    STOPPED: "Stopped",
  };

  const AXIS_CONFIG = {
    x: {
      key: "x",
      label: "X-Axis",
      color: "#38bdf8",
      fillTop: "rgba(56, 189, 248, 0.38)",
      fillBottom: "rgba(56, 189, 248, 0.02)",
      fftColor: "#38bdf8",
      defaultFreqs: [42, 84, 126],
    },
    y: {
      key: "y",
      label: "Y-Axis",
      color: "#4ade80",
      fillTop: "rgba(74, 222, 128, 0.38)",
      fillBottom: "rgba(74, 222, 128, 0.02)",
      fftColor: "#4ade80",
      defaultFreqs: [36, 72, 118],
    },
    z: {
      key: "z",
      label: "Z-Axis",
      color: "#f59e0b",
      fillTop: "rgba(245, 158, 11, 0.38)",
      fillBottom: "rgba(245, 158, 11, 0.02)",
      fftColor: "#f59e0b",
      defaultFreqs: [58, 116, 154],
    },
  };

  const state = {
    activeTab: "tab-capture",
    captureState: STATE.IDLE,
    sequenceCounter: 1,
    completedSessions: 0,
    activeSectionName: null,
    availableSections: [],
    selectedAnalysisFile: "",
    selectedFileRecords: null,
    localRecordedFiles: new Set(),
    spectrumData: null,
    ghostFftData: null,
    axisAnalysisData: {
      x: null,
      y: null,
      z: null,
    },
    callbacks: {
      onStart: null,
      onPause: null,
      onStop: null,
      onTabChange: null,
      onFileSelect: null,
    },
  };

  // DOM References
  const dom = {};

  function cacheDom() {
    dom.tabButtons = Array.from(document.querySelectorAll(".tab-btn"));
    dom.tabPanels = Array.from(document.querySelectorAll(".tab-panel"));
    dom.recordingNameInput = document.getElementById("recording-name-input");
    dom.btnStart = document.getElementById("btn-start");
    dom.btnPause = document.getElementById("btn-pause");
    dom.btnStop = document.getElementById("btn-stop");
    dom.captureStateBadge = document.getElementById("capture-state-badge");
    dom.activeSectionName = document.getElementById("active-section-name");
    dom.sessionCounter = document.getElementById("session-counter");

    // Analysis controls
    dom.analysisFileSelect = document.getElementById("analysis-file-select");
    dom.btnRefreshFiles = document.getElementById("btn-refresh-files");
    dom.analysisSectionPill = document.getElementById("analysis-section-pill");

    // Per-axis Canvases (3 for Full Spectrum, 3 for FFT Ghost Frequencies)
    dom.canvasSpectrumX = document.getElementById("graph-spectrum-x");
    dom.canvasSpectrumY = document.getElementById("graph-spectrum-y");
    dom.canvasSpectrumZ = document.getElementById("graph-spectrum-z");
    dom.canvasFftX = document.getElementById("graph-fft-x");
    dom.canvasFftY = document.getElementById("graph-fft-y");
    dom.canvasFftZ = document.getElementById("graph-fft-z");

    // Legacy single canvas fallback if ever present
    dom.canvasSpectrumLegacy = document.getElementById("graph-full-spectrum");
    dom.canvasGhostFftLegacy = document.getElementById("graph-fft-ghost");
  }

  /**
   * Local fallback section name resolver (mirrors Python SectionManager.resolve_section_path)
   */
  function resolveLocalSectionName() {
    const rawValue = dom.recordingNameInput ? dom.recordingNameInput.value.trim() : "";
    if (rawValue.length > 0) {
      let stripped = rawValue;
      if (stripped.toLowerCase().endsWith(".csv")) {
        stripped = stripped.slice(0, -4).trim();
      }
      let clean = stripped.replace(/[^\w\- ]+/g, "_").replace(/^[ _]+|[ _]+$/g, "");
      if (clean.toLowerCase().endsWith(".csv")) {
        clean = clean.slice(0, -4).replace(/^[ _]+|[ _]+$/g, "");
      }
      if (clean.length > 0) {
        const baseFile = clean + ".csv";
        if (
          !state.localRecordedFiles.has(baseFile) &&
          !state.availableSections.includes(baseFile)
        ) {
          return baseFile;
        }
        let existingCount = 1;
        let maxSuffix = 0;
        const prefix = clean + "_";
        const allKnown = new Set([
          ...Array.from(state.localRecordedFiles),
          ...state.availableSections,
        ]);
        allKnown.forEach((file) => {
          const stem = file.toLowerCase().endsWith(".csv") ? file.slice(0, -4) : file;
          if (stem.startsWith(prefix)) {
            const suffixStr = stem.slice(prefix.length);
            if (/^\d+$/.test(suffixStr)) {
              existingCount += 1;
              const num = parseInt(suffixStr, 10);
              if (num > maxSuffix) {
                maxSuffix = num;
              }
            }
          }
        });
        let nextN = Math.max(existingCount, maxSuffix + 1);
        while (allKnown.has(clean + "_" + nextN + ".csv")) {
          nextN += 1;
        }
        return clean + "_" + nextN + ".csv";
      }
    }
    return state.sequenceCounter + ".csv";
  }

  /**
   * Helper for calling backend API endpoints with graceful fallback
   */
  async function apiRequest(path, options) {
    if (typeof window.fetch !== "function" || window.location.protocol === "file:") {
      return null;
    }
    try {
      const response = await window.fetch(path, {
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        ...options,
      });
      if (!response.ok) {
        return null;
      }
      return await response.json();
    } catch (_err) {
      return null;
    }
  }

  /**
   * Apply status payload from backend `/api/status`, `/api/start`, `/api/pause`, or `/api/stop`
   */
  function applyBackendStatus(data) {
    if (!data || typeof data !== "object") return;
    if (typeof data.state === "string") {
      state.captureState = data.state;
    }
    if ("activeSection" in data) {
      state.activeSectionName = data.activeSection || null;
    }
    if (typeof data.completedSessions === "number") {
      state.completedSessions = data.completedSessions;
    }
    if (typeof data.nextSequenceNumber === "number") {
      state.sequenceCounter = data.nextSequenceNumber;
    }
    if (Array.isArray(data.sections)) {
      state.availableSections = data.sections.slice();
      data.sections.forEach((s) => state.localRecordedFiles.add(s));
      populateSectionDropdown(state.availableSections);
    }
    updateCaptureUI();
  }

  /**
   * Synchronize initial state from `/api/status`
   */
  async function syncStatusFromBackend() {
    const statusData = await apiRequest("/api/status", { method: "GET" });
    if (statusData) {
      applyBackendStatus(statusData);
      if (state.availableSections.length > 0 && !state.selectedAnalysisFile) {
        const defaultFile = state.availableSections[state.availableSections.length - 1];
        await selectAndLoadAnalysisFile(defaultFile);
      }
    } else {
      await loadAvailableSections(true);
    }
  }

  /**
   * Populate `<select id="analysis-file-select">` with available `.csv` files
   * without force-locking the user to only the last session.
   */
  function populateSectionDropdown(sections, preferredFile) {
    if (!dom.analysisFileSelect) return;

    const previousSelection = preferredFile || dom.analysisFileSelect.value || state.selectedAnalysisFile;
    dom.analysisFileSelect.innerHTML = "";

    if (!sections || sections.length === 0) {
      const emptyOpt = document.createElement("option");
      emptyOpt.value = "";
      emptyOpt.textContent = "No CSV recordings available (Preview Mode)";
      dom.analysisFileSelect.appendChild(emptyOpt);
      state.selectedAnalysisFile = "";
      return;
    }

    sections.forEach((filename) => {
      const opt = document.createElement("option");
      opt.value = filename;
      opt.textContent = filename;
      dom.analysisFileSelect.appendChild(opt);
    });

    if (previousSelection && sections.includes(previousSelection)) {
      dom.analysisFileSelect.value = previousSelection;
      state.selectedAnalysisFile = previousSelection;
    } else {
      const chosen = sections[sections.length - 1];
      dom.analysisFileSelect.value = chosen;
      state.selectedAnalysisFile = chosen;
    }
  }

  /**
   * Fetch available CSV sections and refresh dropdown
   */
  async function loadAvailableSections(autoSelectIfEmpty) {
    const data = await apiRequest("/api/sections", { method: "GET" });
    let sections = state.availableSections;
    if (data && Array.isArray(data.sections)) {
      sections = data.sections.slice();
      state.availableSections = sections;
      sections.forEach((s) => state.localRecordedFiles.add(s));
    } else if (state.localRecordedFiles.size > 0 && sections.length === 0) {
      sections = Array.from(state.localRecordedFiles);
      state.availableSections = sections;
    }

    const hadSelection = Boolean(state.selectedAnalysisFile);
    populateSectionDropdown(sections);

    if ((autoSelectIfEmpty || !hadSelection) && state.selectedAnalysisFile) {
      await selectAndLoadAnalysisFile(state.selectedAnalysisFile);
    } else if (state.selectedAnalysisFile) {
      await selectAndLoadAnalysisFile(state.selectedAnalysisFile);
    } else {
      renderAllAxisGraphs();
    }
  }

  /**
   * Fetch Python-computed FFT analysis for a specific CSV file and render the 6 per-axis graphs
   */
  async function selectAndLoadAnalysisFile(filename) {
    if (!filename) {
      state.selectedAnalysisFile = "";
      state.selectedFileRecords = null;
      if (dom.analysisSectionPill) {
        dom.analysisSectionPill.textContent = "Section: Preview Mode";
      }
      renderAllAxisGraphs();
      return;
    }

    state.selectedAnalysisFile = filename;
    if (dom.analysisFileSelect && dom.analysisFileSelect.value !== filename) {
      const exists = Array.from(dom.analysisFileSelect.options).some((o) => o.value === filename);
      if (exists) {
        dom.analysisFileSelect.value = filename;
      }
    }

    const encoded = encodeURIComponent(filename);
    const data = await apiRequest("/api/fft/" + encoded, { method: "GET" });

    if (data && data.axes && typeof data.axes === "object") {
      const count = typeof data.sampleCount === "number" ? data.sampleCount : 0;
      if (dom.analysisSectionPill) {
        dom.analysisSectionPill.textContent =
          "Section: " + filename + " (" + count + (count === 1 ? " sample)" : " samples)");
      }
      applyBackendFftAnalysis(data.axes, filename);
    } else {
      if (dom.analysisSectionPill) {
        dom.analysisSectionPill.textContent = "Section: " + filename;
      }
      state.axisAnalysisData = { x: null, y: null, z: null };
    }

    renderAllAxisGraphs();

    if (typeof state.callbacks.onFileSelect === "function") {
      state.callbacks.onFileSelect({
        filename,
        sampleCount: data && typeof data.sampleCount === "number" ? data.sampleCount : 0,
        axes: state.axisAnalysisData,
      });
    }
  }

  /**
   * Switch between "tab-capture" and "tab-analysis"
   */
  function switchTab(targetTabId) {
    if (!targetTabId) return;
    state.activeTab = targetTabId;

    dom.tabButtons.forEach((btn) => {
      const isMatch = btn.getAttribute("data-target") === targetTabId;
      btn.classList.toggle("active", isMatch);
      btn.setAttribute("aria-selected", isMatch ? "true" : "false");
    });

    dom.tabPanels.forEach((panel) => {
      const isMatch = panel.id === targetTabId;
      panel.classList.toggle("active", isMatch);
      if (isMatch) {
        panel.removeAttribute("hidden");
      } else {
        panel.setAttribute("hidden", "");
      }
    });

    if (targetTabId === "tab-analysis") {
      loadAvailableSections(false);
      renderAllAxisGraphs();
    }

    if (typeof state.callbacks.onTabChange === "function") {
      state.callbacks.onTabChange(targetTabId);
    }
  }

  /**
   * Update UI controls and status badges based on current capture state
   */
  function updateCaptureUI() {
    const currentState = state.captureState;

    if (dom.captureStateBadge) {
      dom.captureStateBadge.textContent = currentState;
      dom.captureStateBadge.className = "state-badge";
      if (currentState === STATE.IDLE) {
        dom.captureStateBadge.classList.add("state-idle");
      } else if (currentState === STATE.RECORDING) {
        dom.captureStateBadge.classList.add("state-recording");
      } else if (currentState === STATE.PAUSED) {
        dom.captureStateBadge.classList.add("state-paused");
      } else if (currentState === STATE.STOPPED) {
        dom.captureStateBadge.classList.add("state-stopped");
      }
    }

    if (dom.activeSectionName) {
      if (state.activeSectionName) {
        dom.activeSectionName.textContent = state.activeSectionName;
      } else {
        dom.activeSectionName.textContent =
          "None (Next auto-ID: #" + state.sequenceCounter + ")";
      }
    }

    if (dom.analysisSectionPill && !state.selectedAnalysisFile) {
      if (state.activeSectionName) {
        dom.analysisSectionPill.textContent = "Section: " + state.activeSectionName;
      } else {
        dom.analysisSectionPill.textContent = "Section: Preview Mode";
      }
    }

    if (dom.sessionCounter) {
      dom.sessionCounter.textContent = state.completedSessions + " completed";
    }

    if (!dom.btnStart || !dom.btnPause || !dom.btnStop || !dom.recordingNameInput) {
      return;
    }

    if (currentState === STATE.RECORDING) {
      dom.btnStart.disabled = true;
      dom.btnPause.disabled = false;
      dom.btnStop.disabled = false;
      dom.recordingNameInput.disabled = true;
      dom.btnStart.innerHTML = '<span class="btn-icon">&#9654;</span> Start';
    } else if (currentState === STATE.PAUSED) {
      dom.btnStart.disabled = false;
      dom.btnPause.disabled = true;
      dom.btnStop.disabled = false;
      dom.recordingNameInput.disabled = true;
      dom.btnStart.innerHTML = '<span class="btn-icon">&#9654;</span> Resume';
    } else {
      // IDLE or STOPPED
      dom.btnStart.disabled = false;
      dom.btnPause.disabled = true;
      dom.btnStop.disabled = true;
      dom.recordingNameInput.disabled = false;
      dom.btnStart.innerHTML = '<span class="btn-icon">&#9654;</span> Start';
    }
  }

  /**
   * Handle Start / Resume click
   */
  async function handleStart() {
    const isResuming = state.captureState === STATE.PAUSED;
    const rawName = dom.recordingNameInput ? dom.recordingNameInput.value.trim() : "";

    // Optimistic local state update so synchronous callers and offline mode work immediately
    if (!isResuming) {
      state.activeSectionName = resolveLocalSectionName();
      state.localRecordedFiles.add(state.activeSectionName);
      if (!state.availableSections.includes(state.activeSectionName)) {
        state.availableSections.push(state.activeSectionName);
      }
    }
    state.captureState = STATE.RECORDING;
    updateCaptureUI();

    const apiData = await apiRequest("/api/start", {
      method: "POST",
      body: JSON.stringify({ sectionName: rawName || null }),
    });

    if (apiData) {
      applyBackendStatus(apiData);
    } else {
      populateSectionDropdown(state.availableSections);
    }

    if (typeof state.callbacks.onStart === "function") {
      state.callbacks.onStart({
        sectionName: state.activeSectionName,
        resumed: isResuming,
        sequenceNumber: state.sequenceCounter,
      });
    }
  }

  /**
   * Handle Pause click
   */
  async function handlePause() {
    if (state.captureState !== STATE.RECORDING) return;
    state.captureState = STATE.PAUSED;
    updateCaptureUI();

    const apiData = await apiRequest("/api/pause", {
      method: "POST",
      body: JSON.stringify({}),
    });

    if (apiData) {
      applyBackendStatus(apiData);
    }

    if (typeof state.callbacks.onPause === "function") {
      state.callbacks.onPause({
        sectionName: state.activeSectionName,
      });
    }
  }

  /**
   * Handle Stop click
   * CRITICAL: Do NOT clear `dom.recordingNameInput.value` when Stop is clicked!
   */
  async function handleStop() {
    if (state.captureState !== STATE.RECORDING && state.captureState !== STATE.PAUSED) {
      return;
    }
    const finishedSection = state.activeSectionName;
    state.captureState = STATE.STOPPED;
    state.completedSessions += 1;
    if (finishedSection && /^\d+(\.csv)?$/i.test(finishedSection)) {
      state.sequenceCounter += 1;
    }
    // Intentionally preserve dom.recordingNameInput.value so subsequent Starts create name_1.csv, name_2.csv, etc.
    updateCaptureUI();

    const apiData = await apiRequest("/api/stop", {
      method: "POST",
      body: JSON.stringify({}),
    });

    if (apiData) {
      applyBackendStatus(apiData);
    }

    await loadAvailableSections(false);

    if (typeof state.callbacks.onStop === "function") {
      state.callbacks.onStop({
        sectionName: finishedSection,
        completedSessions: state.completedSessions,
      });
    }
  }

  /**
   * Store per-axis spectral & FFT analysis data computed by the Python backend (fft.py)
   */
  function applyBackendFftAnalysis(axesPayload, filename) {
    ["x", "y", "z"].forEach((axis) => {
      const axisResult = axesPayload && axesPayload[axis];
      if (!axisResult || axisResult.empty) {
        state.axisAnalysisData[axis] = {
          empty: true,
          filename: filename || "selected file",
          maxFrequency: (axisResult && axisResult.maxFrequency) || 500,
          spectrumPoints: [],
          fftPoints: [],
          maxAmplitude: 1.0,
          maxMagnitude: 1.0,
          threshold: 0.35,
          ghostPeaks: [],
        };
      } else {
        state.axisAnalysisData[axis] = {
          empty: false,
          filename: axisResult.filename || filename,
          maxFrequency: axisResult.maxFrequency || 500,
          spectrumPoints: Array.isArray(axisResult.spectrumPoints) ? axisResult.spectrumPoints : [],
          fftPoints: Array.isArray(axisResult.fftPoints) ? axisResult.fftPoints : [],
          maxAmplitude: axisResult.maxAmplitude || 1.0,
          maxMagnitude: axisResult.maxMagnitude || 1.0,
          threshold: typeof axisResult.threshold === "number" ? axisResult.threshold : 0.35,
          ghostPeaks: Array.isArray(axisResult.ghostPeaks) ? axisResult.ghostPeaks : [],
        };
      }
    });
  }

  /**
   * Generate default illustrative broadband frequency spectrum data (0 - 200 Hz) for a given axis
   */
  function getDefaultSpectrumData(axisKey) {
    const cfg = AXIS_CONFIG[axisKey] || AXIS_CONFIG.x;
    const [f1, f2, f3] = cfg.defaultFreqs;
    const points = [];
    const maxFreq = 200;
    for (let f = 0; f <= maxFreq; f += 1) {
      const baseNoise = 0.04 + 0.02 * Math.sin(f * 0.37) + 0.015 * Math.cos(f * 0.83);
      const peak1 = 0.72 * Math.exp(-Math.pow((f - f1) / 5.5, 2));
      const peak2 = 0.48 * Math.exp(-Math.pow((f - f2) / 6.5, 2));
      const peak3 = 0.31 * Math.exp(-Math.pow((f - f3) / 8.0, 2));
      const highFreqChatter = 0.12 * Math.exp(-Math.pow((f - 165) / 11.0, 2));
      const amplitude = Math.max(0.01, baseNoise + peak1 + peak2 + peak3 + highFreqChatter);
      points.push({ frequency: f, amplitude: Number(amplitude.toFixed(4)) });
    }
    return {
      points,
      maxFrequency: maxFreq,
      maxAmplitude: 1.0,
      unitX: "Hz",
      unitY: "g",
    };
  }

  /**
   * Generate default illustrative FFT Ghost Frequency data for a given axis
   */
  function getDefaultGhostFftData(axisKey) {
    const cfg = AXIS_CONFIG[axisKey] || AXIS_CONFIG.x;
    const [f1, f2, f3] = cfg.defaultFreqs;
    const points = [];
    const maxFreq = 200;
    for (let f = 0; f <= maxFreq; f += 1) {
      const floor = 0.02 + 0.008 * Math.abs(Math.sin(f * 0.5));
      const p1 = 0.88 * Math.exp(-Math.pow((f - f1) / 2.2, 2));
      const p2 = 0.64 * Math.exp(-Math.pow((f - f2) / 2.6, 2));
      const p3 = 0.39 * Math.exp(-Math.pow((f - f3) / 3.0, 2));
      const magnitude = Math.min(1.0, floor + p1 + p2 + p3);
      points.push({ frequency: f, magnitude: Number(magnitude.toFixed(4)) });
    }
    return {
      points,
      maxFrequency: maxFreq,
      maxMagnitude: 1.0,
      threshold: 0.35,
      ghostPeaks: [
        { frequency: f1, magnitude: 0.9, label: f1 + " Hz (" + cfg.label + " Primary)" },
        { frequency: f2, magnitude: 0.66, label: f2 + " Hz (2nd Harmonic)" },
        { frequency: f3, magnitude: 0.41, label: f3 + " Hz (Belt / Frame)" },
      ],
    };
  }

  /**
   * Helper to draw coordinate axes, gridlines, and labels on a canvas
   */
  function drawAxesAndGrid(ctx, width, height, padding, options) {
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;

    ctx.clearRect(0, 0, width, height);

    // Background fill
    ctx.fillStyle = "#0b1120";
    ctx.fillRect(0, 0, width, height);

    // Grid lines
    ctx.strokeStyle = "rgba(148, 163, 184, 0.14)";
    ctx.lineWidth = 1;
    ctx.font = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    ctx.fillStyle = "#94a3b8";

    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const ratio = i / yTicks;
      const y = padding.top + plotHeight - ratio * plotHeight;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotWidth, y);
      ctx.stroke();

      const val = (ratio * options.maxY).toFixed(2);
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(val + " " + options.unitY, padding.left - 8, y);
    }

    const xTicks = 8;
    for (let i = 0; i <= xTicks; i++) {
      const ratio = i / xTicks;
      const x = padding.left + ratio * plotWidth;
      ctx.beginPath();
      ctx.moveTo(x, padding.top);
      ctx.lineTo(x, padding.top + plotHeight);
      ctx.stroke();

      const freqVal = Math.round(ratio * options.maxX);
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(freqVal + " " + options.unitX, x, padding.top + plotHeight + 8);
    }

    // Primary axes border
    ctx.strokeStyle = "#475569";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(padding.left, padding.top);
    ctx.lineTo(padding.left, padding.top + plotHeight);
    ctx.lineTo(padding.left + plotWidth, padding.top + plotHeight);
    ctx.stroke();

    return { plotWidth, plotHeight };
  }

  /**
   * Draw empty-state message overlay on a canvas when CSV has 0 rows
   */
  function drawEmptyMessage(ctx, padding, plotWidth, plotHeight, message) {
    ctx.save();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "600 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(
      message,
      padding.left + plotWidth / 2,
      padding.top + plotHeight / 2
    );
    ctx.restore();
  }

  /**
   * Render a single axis Full Frequency Spectrum canvas
   */
  function renderAxisSpectrumCanvas(canvas, axisKey, customData) {
    if (!canvas) return;
    const cfg = AXIS_CONFIG[axisKey] || AXIS_CONFIG.x;
    const axisData = state.axisAnalysisData[axisKey];

    let data;
    if (customData) {
      data = customData;
    } else if (axisData) {
      data = {
        empty: axisData.empty,
        filename: axisData.filename,
        points: axisData.spectrumPoints,
        maxFrequency: axisData.maxFrequency,
        maxAmplitude: axisData.maxAmplitude,
        unitX: "Hz",
        unitY: "g",
      };
    } else {
      data = getDefaultSpectrumData(axisKey);
    }

    const ctx = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;
    const padding = { top: 22, right: 24, bottom: 32, left: 62 };

    const maxFreq = data.maxFrequency || 200;
    const maxAmp = data.maxAmplitude || 1.0;

    const { plotWidth, plotHeight } = drawAxesAndGrid(ctx, width, height, padding, {
      maxX: maxFreq,
      maxY: maxAmp,
      unitX: data.unitX || "Hz",
      unitY: data.unitY || "g",
    });

    if (data.empty || !data.points || data.points.length === 0) {
      const label = data.filename
        ? "No samples in " + data.filename + " (" + cfg.label + ")"
        : "No samples available (" + cfg.label + ")";
      drawEmptyMessage(ctx, padding, plotWidth, plotHeight, label);
      return;
    }

    const gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotHeight);
    gradient.addColorStop(0, cfg.fillTop);
    gradient.addColorStop(1, cfg.fillBottom);

    ctx.beginPath();
    data.points.forEach((pt, idx) => {
      const x = padding.left + (pt.frequency / maxFreq) * plotWidth;
      const y = padding.top + plotHeight - Math.min(1, pt.amplitude / maxAmp) * plotHeight;
      if (idx === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });

    ctx.lineTo(padding.left + plotWidth, padding.top + plotHeight);
    ctx.lineTo(padding.left, padding.top + plotHeight);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    data.points.forEach((pt, idx) => {
      const x = padding.left + (pt.frequency / maxFreq) * plotWidth;
      const y = padding.top + plotHeight - Math.min(1, pt.amplitude / maxAmp) * plotHeight;
      if (idx === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.strokeStyle = cfg.color;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  /**
   * Render a single axis Fourier Transform (FFT) Ghost Frequencies canvas
   */
  function renderAxisGhostFftCanvas(canvas, axisKey, customData) {
    if (!canvas) return;
    const cfg = AXIS_CONFIG[axisKey] || AXIS_CONFIG.x;
    const axisData = state.axisAnalysisData[axisKey];

    let data;
    if (customData) {
      data = customData;
    } else if (axisData) {
      data = {
        empty: axisData.empty,
        filename: axisData.filename,
        points: axisData.fftPoints,
        maxFrequency: axisData.maxFrequency,
        maxMagnitude: axisData.maxMagnitude,
        threshold: axisData.threshold,
        ghostPeaks: axisData.ghostPeaks,
      };
    } else {
      data = getDefaultGhostFftData(axisKey);
    }

    const ctx = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;
    const padding = { top: 24, right: 24, bottom: 32, left: 62 };

    const maxFreq = data.maxFrequency || 200;
    const maxMag = data.maxMagnitude || 1.0;

    const { plotWidth, plotHeight } = drawAxesAndGrid(ctx, width, height, padding, {
      maxX: maxFreq,
      maxY: maxMag,
      unitX: "Hz",
      unitY: "mag",
    });

    if (data.empty || !data.points || data.points.length === 0) {
      const label = data.filename
        ? "No samples in " + data.filename + " (" + cfg.label + ")"
        : "No samples available (" + cfg.label + ")";
      drawEmptyMessage(ctx, padding, plotWidth, plotHeight, label);
      return;
    }

    // Draw Ghost Resonance Threshold line
    if (typeof data.threshold === "number") {
      const thresholdY =
        padding.top + plotHeight - Math.min(1, data.threshold / maxMag) * plotHeight;
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = "rgba(239, 68, 68, 0.7)";
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.moveTo(padding.left, thresholdY);
      ctx.lineTo(padding.left + plotWidth, thresholdY);
      ctx.stroke();

      ctx.fillStyle = "#f87171";
      ctx.font = "600 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(
        "Ghost Ringing Threshold (" + data.threshold.toFixed(2) + ")",
        padding.left + plotWidth - 6,
        thresholdY - 4
      );
      ctx.restore();
    }

    // Draw FFT spectrum curve
    ctx.beginPath();
    data.points.forEach((pt, idx) => {
      const x = padding.left + (pt.frequency / maxFreq) * plotWidth;
      const y = padding.top + plotHeight - Math.min(1, pt.magnitude / maxMag) * plotHeight;
      if (idx === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.strokeStyle = cfg.fftColor;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Highlight detected ghost resonance peaks
    if (Array.isArray(data.ghostPeaks)) {
      data.ghostPeaks.forEach((peak) => {
        const px = padding.left + (peak.frequency / maxFreq) * plotWidth;
        const py = padding.top + plotHeight - Math.min(1, peak.magnitude / maxMag) * plotHeight;

        ctx.beginPath();
        ctx.arc(px, py, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = "#ef4444";
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "#ffffff";
        ctx.stroke();

        ctx.font = "600 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
        ctx.fillStyle = "#f8fafc";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(
          peak.label || Math.round(peak.frequency) + " Hz",
          px + 8,
          Math.max(padding.top + 10, py - 4)
        );
      });
    }
  }

  /**
   * Render all 3 Full Frequency Spectrum canvases (X, Y, Z)
   */
  function renderSpectrum(customData) {
    if (customData) {
      state.spectrumData = customData;
    }
    renderAxisSpectrumCanvas(dom.canvasSpectrumX, "x", customData);
    renderAxisSpectrumCanvas(dom.canvasSpectrumY, "y", customData);
    renderAxisSpectrumCanvas(dom.canvasSpectrumZ, "z", customData);

    if (dom.canvasSpectrumLegacy) {
      renderAxisSpectrumCanvas(dom.canvasSpectrumLegacy, "x", customData);
    }
  }

  /**
   * Render all 3 FFT Ghost Frequency canvases (X, Y, Z)
   */
  function renderGhostFFT(customData) {
    if (customData) {
      state.ghostFftData = customData;
    }
    renderAxisGhostFftCanvas(dom.canvasFftX, "x", customData);
    renderAxisGhostFftCanvas(dom.canvasFftY, "y", customData);
    renderAxisGhostFftCanvas(dom.canvasFftZ, "z", customData);

    if (dom.canvasGhostFftLegacy) {
      renderAxisGhostFftCanvas(dom.canvasGhostFftLegacy, "x", customData);
    }
  }

  /**
   * Render all 6 per-axis canvases
   */
  function renderAllAxisGraphs() {
    renderSpectrum();
    renderGhostFFT();
  }

  function bindEvents() {
    dom.tabButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-target");
        switchTab(target);
      });
    });

    if (dom.btnStart) {
      dom.btnStart.addEventListener("click", handleStart);
    }
    if (dom.btnPause) {
      dom.btnPause.addEventListener("click", handlePause);
    }
    if (dom.btnStop) {
      dom.btnStop.addEventListener("click", handleStop);
    }

    if (dom.analysisFileSelect) {
      dom.analysisFileSelect.addEventListener("change", (e) => {
        const selectedFile = e.target.value;
        selectAndLoadAnalysisFile(selectedFile);
      });
    }

    if (dom.btnRefreshFiles) {
      dom.btnRefreshFiles.addEventListener("click", () => {
        loadAvailableSections(false);
      });
    }
  }

  function init() {
    cacheDom();
    bindEvents();
    state.spectrumData = getDefaultSpectrumData("x");
    state.ghostFftData = getDefaultGhostFftData("x");
    updateCaptureUI();
    renderAllAxisGraphs();
    syncStatusFromBackend();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose public controller on window.SupervisoryUI
  window.SupervisoryUI = {
    switchTab,
    startRecording: handleStart,
    pauseRecording: handlePause,
    stopRecording: handleStop,
    loadAvailableSections,
    selectAnalysisFile: selectAndLoadAnalysisFile,
    renderSpectrum,
    renderGhostFFT,
    renderAllAxisGraphs,
    getState: function () {
      return {
        activeTab: state.activeTab,
        captureState: state.captureState,
        sequenceCounter: state.sequenceCounter,
        completedSessions: state.completedSessions,
        activeSectionName: state.activeSectionName,
        availableSections: state.availableSections.slice(),
        selectedAnalysisFile: state.selectedAnalysisFile,
      };
    },
    on: function (eventName, callback) {
      if (eventName in state.callbacks) {
        state.callbacks[eventName] = callback;
      }
    },
  };
})();
