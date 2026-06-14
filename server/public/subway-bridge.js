(function () {
  "use strict";

  var READ_INTERVAL_MS = 250;
  var TEMPLATE_WIDTH = 18;
  var TEMPLATE_HEIGHT = 28;
  var MAX_SCORE = 999999999;
  var START_JUMP_LIMIT = 10000;
  var FONT_STACKS = [
    "900 30px Arial Black",
    "900 30px Impact",
    "900 30px Trebuchet MS",
    "900 30px Torus, Arial Black"
  ];

  var state = {
    phase: "loading",
    score: 0,
    bestScore: readBestScore(),
    finalScore: 0,
    scoreSource: "none"
  };

  var ui = {};
  var lastAcceptedScoreAt = 0;
  var readerTimer = 0;
  var templateCache = null;
  var scratch = document.createElement("canvas");
  var scratchContext = scratch.getContext("2d", { willReadFrequently: true });
  var installed = false;

  function readBestScore() {
    var value = 0;
    try {
      value = parseInt(sessionStorage.getItem("subway.bestScore") || "0", 10);
    } catch (error) {
      value = 0;
    }
    return Number.isFinite(value) ? value : 0;
  }

  function writeBestScore(value) {
    try {
      sessionStorage.setItem("subway.bestScore", String(value));
    } catch (error) {
      // Session storage is a convenience only.
    }
  }

  function cloneState() {
    return {
      phase: state.phase,
      score: state.score,
      bestScore: state.bestScore,
      finalScore: state.finalScore,
      scoreSource: state.scoreSource
    };
  }

  function emit(name, detail) {
    window.dispatchEvent(new CustomEvent("subway:" + name, {
      detail: detail
    }));
  }

  function formatScore(value) {
    return Math.max(0, Math.floor(value || 0)).toLocaleString("en-US");
  }

  function mountUI() {
    if (installed) return;
    installed = true;

    var root = document.createElement("div");
    root.id = "subway-custom-ui";
    root.setAttribute("data-phase", state.phase);
    root.innerHTML = [
      '<div class="subway-mask" aria-hidden="true"></div>',
      '<div class="subway-hud" aria-live="polite">',
      '  <div class="subway-score-block">',
      '    <span class="subway-label">Score</span>',
      '    <strong class="subway-score" data-subway-score>0</strong>',
      '  </div>',
      '  <div class="subway-run-meta">',
      '    <span data-subway-phase>Loading</span>',
      '    <span data-subway-source>none</span>',
      '  </div>',
      '</div>',
      '<div class="subway-end" role="dialog" aria-modal="true" aria-labelledby="subway-end-title">',
      '  <div class="subway-end-panel">',
      '    <p class="subway-kicker">Run ended</p>',
      '    <h1 id="subway-end-title" data-subway-final>0</h1>',
      '    <div class="subway-end-stats">',
      '      <div><span>Best</span><strong data-subway-best>0</strong></div>',
      '      <div><span>Source</span><strong data-subway-final-source>none</strong></div>',
      '    </div>',
      '    <button type="button" class="subway-restart" data-subway-action="restart">Restart</button>',
      '  </div>',
      '</div>'
    ].join("");

    document.body.appendChild(root);

    ui.root = root;
    ui.score = root.querySelector("[data-subway-score]");
    ui.phase = root.querySelector("[data-subway-phase]");
    ui.source = root.querySelector("[data-subway-source]");
    ui.finalScore = root.querySelector("[data-subway-final]");
    ui.best = root.querySelector("[data-subway-best]");
    ui.finalSource = root.querySelector("[data-subway-final-source]");

    root.querySelector('[data-subway-action="restart"]').addEventListener("click", function (event) {
      event.preventDefault();
      window.SubwayBridge.restart();
    });

    renderUI();
  }

  function renderUI() {
    if (!ui.root) return;
    ui.root.setAttribute("data-phase", state.phase);
    ui.root.setAttribute("data-score", String(state.score));
    ui.root.setAttribute("data-best-score", String(state.bestScore));
    ui.root.setAttribute("data-final-score", String(state.finalScore));
    ui.root.setAttribute("data-score-source", state.scoreSource);
    ui.score.textContent = formatScore(state.score);
    ui.phase.textContent = state.phase;
    ui.source.textContent = state.scoreSource;
    ui.finalScore.textContent = formatScore(state.finalScore || state.score);
    ui.best.textContent = formatScore(state.bestScore);
    ui.finalSource.textContent = state.scoreSource;
  }

  function resetRun(source) {
    state.score = 0;
    state.finalScore = 0;
    state.scoreSource = source || "reset";
    lastAcceptedScoreAt = Date.now();
    emit("score", { score: state.score, source: state.scoreSource });
  }

  function finishRun(source) {
    state.finalScore = Math.max(state.finalScore || 0, state.score || 0);
    if (state.finalScore > state.bestScore) {
      state.bestScore = state.finalScore;
      writeBestScore(state.bestScore);
    }
    emit("gameover", {
      finalScore: state.finalScore,
      bestScore: state.bestScore,
      source: source || state.scoreSource
    });
  }

  function setPhase(phase, source) {
    if (!phase || phase === state.phase) return;

    var previous = state.phase;
    if (phase === "running" && previous !== "running") {
      resetRun(source || "phase");
    }

    state.phase = phase;
    if (phase === "gameover") {
      finishRun(source || "phase");
    }

    emit("phase", { phase: state.phase, previous: previous, source: source || "bridge" });
    renderUI();
  }

  function isPlausibleScore(value) {
    if (!Number.isFinite(value) || value < 0 || value > MAX_SCORE) return false;
    if (state.phase !== "running") return value >= state.score;
    if (value < state.score) return false;

    var now = Date.now();
    var elapsed = Math.max(0.25, (now - (lastAcceptedScoreAt || now)) / 1000);
    if (state.score === 0 && value > START_JUMP_LIMIT && elapsed < 3) return false;

    var maxDelta = 8000 + elapsed * 120000;
    return value - state.score <= maxDelta;
  }

  function setScore(value, source, options) {
    var score = parseInt(value, 10);
    if (!isPlausibleScore(score) && !(options && options.force)) return false;
    if (score === state.score && source === state.scoreSource) return true;

    state.score = score;
    state.scoreSource = source || "unknown";
    lastAcceptedScoreAt = Date.now();

    if (state.phase === "gameover") {
      state.finalScore = Math.max(state.finalScore, score);
      if (state.finalScore > state.bestScore) {
        state.bestScore = state.finalScore;
        writeBestScore(state.bestScore);
      }
    }

    emit("score", { score: state.score, source: state.scoreSource });
    renderUI();
    return true;
  }

  function extractScore(value, depth) {
    if (depth > 4 || value == null) return null;

    if (typeof value === "number") {
      return Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
    }

    if (typeof value === "string") {
      var trimmed = value.trim();
      if (!trimmed) return null;

      if ((trimmed.charAt(0) === "{" && trimmed.charAt(trimmed.length - 1) === "}") ||
          (trimmed.charAt(0) === "[" && trimmed.charAt(trimmed.length - 1) === "]")) {
        try {
          return extractScore(JSON.parse(trimmed), depth + 1);
        } catch (error) {
          // Fall through to regex parsing.
        }
      }

      var match = trimmed.match(/(?:score|points|meters|distance|highscore)[^0-9]{0,16}([0-9][0-9, ]{0,12})/i);
      if (match) return parseInt(match[1].replace(/[^0-9]/g, ""), 10);
      return null;
    }

    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i += 1) {
        var found = extractScore(value[i], depth + 1);
        if (found != null) return found;
      }
      return null;
    }

    if (typeof value === "object") {
      var best = null;
      Object.keys(value).forEach(function (key) {
        if (best != null) return;
        var lower = key.toLowerCase();
        if (/(^|_|\b)(score|points|meters|distance|highscore)(\b|_|$)/.test(lower)) {
          best = extractScore(value[key], depth + 1);
        }
      });
      if (best != null) return best;

      Object.keys(value).some(function (key) {
        best = extractScore(value[key], depth + 1);
        return best != null;
      });
      return best;
    }

    return null;
  }

  function handlePokiCall(methodName, args) {
    var score = extractScore(Array.prototype.slice.call(args), 0);
    if (score != null) setScore(score, "hook:" + methodName);

    if (methodName === "gameplayStart" || methodName === "roundStart") {
      setPhase("running", "hook:" + methodName);
    } else if (methodName === "gameplayStop" || methodName === "roundEnd") {
      setPhase("gameover", "hook:" + methodName);
    } else if (methodName === "gameLoadingFinished" || methodName === "gameInteractive") {
      if (state.phase === "loading") setPhase("ready", "hook:" + methodName);
    }
  }

  function wrapSdkMethod(sdk, methodName) {
    var original = sdk && sdk[methodName];
    if (typeof original !== "function" || original.__subwayWrappedMethod) return;

    var wrapped = function () {
      handlePokiCall(methodName, arguments);
      return original.apply(this, arguments);
    };
    wrapped.__subwayWrappedMethod = true;
    wrapped.__subwayOriginal = original;
    sdk[methodName] = wrapped;
  }

  function wrapPokiSdk(sdk) {
    if (!sdk) return sdk;
    [
      "customEvent",
      "gameInteractive",
      "gameLoadingFinished",
      "gameplayStart",
      "gameplayStop",
      "roundEnd",
      "roundStart",
      "sendHighscore"
    ].forEach(function (methodName) {
      wrapSdkMethod(sdk, methodName);
    });
    return sdk;
  }

  function installPokiHook() {
    var descriptor = Object.getOwnPropertyDescriptor(window, "PokiSDK");
    var pokiValue = descriptor && descriptor.get ? descriptor.get.call(window) : window.PokiSDK;

    if (!descriptor || descriptor.configurable) {
      Object.defineProperty(window, "PokiSDK", {
        configurable: true,
        enumerable: true,
        get: function () {
          return pokiValue;
        },
        set: function (nextValue) {
          pokiValue = wrapPokiSdk(nextValue);
        }
      });
    }

    pokiValue = wrapPokiSdk(pokiValue);
    window.setInterval(function () {
      wrapPokiSdk(window.PokiSDK);
    }, 1000);
  }

  function installUnityBridgeHook() {
    var descriptor = Object.getOwnPropertyDescriptor(window, "initPokiBridge");
    var bridgeValue = descriptor && descriptor.get ? descriptor.get.call(window) : window.initPokiBridge;

    if (descriptor && !descriptor.configurable) return;

    Object.defineProperty(window, "initPokiBridge", {
      configurable: true,
      enumerable: true,
      get: function () {
        return bridgeValue;
      },
      set: function (nextValue) {
        if (typeof nextValue !== "function") {
          bridgeValue = nextValue;
          return;
        }

        bridgeValue = function (name) {
          window.SubwayBridge.pokiBridgeName = name;
          return nextValue.apply(this, arguments);
        };
      }
    });

    if (bridgeValue) {
      window.initPokiBridge = bridgeValue;
    }
  }

  function getGameCanvas() {
    return document.querySelector("#game canvas") || document.querySelector("canvas");
  }

  function buildTemplates() {
    if (templateCache) return templateCache;

    templateCache = {};
    var canvas = document.createElement("canvas");
    var context = canvas.getContext("2d", { willReadFrequently: true });
    canvas.width = 48;
    canvas.height = 56;

    for (var digit = 0; digit <= 9; digit += 1) {
      templateCache[digit] = [];
      FONT_STACKS.forEach(function (font) {
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = "#000";
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = "#fff";
        context.font = font;
        context.textAlign = "center";
        context.textBaseline = "middle";
        context.fillText(String(digit), canvas.width / 2, canvas.height / 2 + 2);
        templateCache[digit].push(normalizeImageData(
          context.getImageData(0, 0, canvas.width, canvas.height),
          TEMPLATE_WIDTH,
          TEMPLATE_HEIGHT
        ));
      });
    }

    return templateCache;
  }

  function normalizeImageData(imageData, outWidth, outHeight) {
    var binary = thresholdImageData(imageData, true);
    return normalizeMask(binary.mask, binary.width, binary.height, outWidth, outHeight);
  }

  function thresholdImageData(imageData, darkBackground) {
    var data = imageData.data;
    var width = imageData.width;
    var height = imageData.height;
    var mask = new Uint8Array(width * height);
    var lumas = new Uint8Array(width * height);
    var sum = 0;

    for (var i = 0; i < data.length; i += 4) {
      var pixel = i / 4;
      var r = data[i];
      var g = data[i + 1];
      var b = data[i + 2];
      var luma = Math.round(0.2126 * r + 0.7152 * g + 0.0722 * b);
      lumas[pixel] = luma;
      sum += luma;
    }

    var mean = sum / lumas.length;
    var variance = 0;
    for (var j = 0; j < lumas.length; j += 1) {
      variance += Math.pow(lumas[j] - mean, 2);
    }

    var deviation = Math.sqrt(variance / Math.max(1, lumas.length));
    var threshold = darkBackground ? 20 : Math.max(150, mean + deviation * 0.75);

    for (var k = 0; k < data.length; k += 4) {
      var index = k / 4;
      var rr = data[k];
      var gg = data[k + 1];
      var bb = data[k + 2];
      var aa = data[k + 3];
      var lum = lumas[index];
      var yellow = rr > 165 && gg > 130 && bb < 130;
      var white = rr > 175 && gg > 175 && bb > 175;
      var bright = lum >= threshold;
      mask[index] = aa > 80 && (yellow || white || bright) ? 1 : 0;
    }

    return { mask: mask, width: width, height: height };
  }

  function normalizeMask(mask, width, height, outWidth, outHeight) {
    var bounds = getBounds(mask, width, height);
    var normalized = new Uint8Array(outWidth * outHeight);
    if (!bounds) return normalized;

    var sourceWidth = Math.max(1, bounds.maxX - bounds.minX + 1);
    var sourceHeight = Math.max(1, bounds.maxY - bounds.minY + 1);

    for (var y = 0; y < outHeight; y += 1) {
      for (var x = 0; x < outWidth; x += 1) {
        var sx = bounds.minX + Math.floor((x + 0.5) * sourceWidth / outWidth);
        var sy = bounds.minY + Math.floor((y + 0.5) * sourceHeight / outHeight);
        normalized[y * outWidth + x] = mask[sy * width + sx] ? 1 : 0;
      }
    }

    return normalized;
  }

  function getBounds(mask, width, height) {
    var minX = width;
    var minY = height;
    var maxX = -1;
    var maxY = -1;

    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        if (!mask[y * width + x]) continue;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }

    if (maxX < minX || maxY < minY) return null;
    return { minX: minX, minY: minY, maxX: maxX, maxY: maxY };
  }

  function findComponents(mask, width, height) {
    var visited = new Uint8Array(width * height);
    var components = [];
    var stack = [];

    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        var start = y * width + x;
        if (!mask[start] || visited[start]) continue;

        var minX = x;
        var maxX = x;
        var minY = y;
        var maxY = y;
        var count = 0;
        stack.length = 0;
        stack.push(start);
        visited[start] = 1;

        while (stack.length) {
          var index = stack.pop();
          var px = index % width;
          var py = (index - px) / width;
          count += 1;
          if (px < minX) minX = px;
          if (px > maxX) maxX = px;
          if (py < minY) minY = py;
          if (py > maxY) maxY = py;

          for (var dy = -1; dy <= 1; dy += 1) {
            for (var dx = -1; dx <= 1; dx += 1) {
              if (!dx && !dy) continue;
              var nx = px + dx;
              var ny = py + dy;
              if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
              var next = ny * width + nx;
              if (mask[next] && !visited[next]) {
                visited[next] = 1;
                stack.push(next);
              }
            }
          }
        }

        var boxWidth = maxX - minX + 1;
        var boxHeight = maxY - minY + 1;
        var aspect = boxWidth / Math.max(1, boxHeight);
        if (count >= 12 && boxHeight >= 12 && boxWidth >= 3 && aspect <= 1.25) {
          components.push({
            minX: minX,
            maxX: maxX,
            minY: minY,
            maxY: maxY,
            width: boxWidth,
            height: boxHeight,
            area: count,
            cx: minX + boxWidth / 2,
            cy: minY + boxHeight / 2
          });
        }
      }
    }

    return components;
  }

  function cropMask(mask, width, box) {
    var cropped = new Uint8Array(box.width * box.height);
    for (var y = 0; y < box.height; y += 1) {
      for (var x = 0; x < box.width; x += 1) {
        cropped[y * box.width + x] = mask[(box.minY + y) * width + box.minX + x];
      }
    }
    return cropped;
  }

  function matchDigit(mask) {
    var templates = buildTemplates();
    var bestDigit = null;
    var bestScore = -1;

    Object.keys(templates).forEach(function (digit) {
      templates[digit].forEach(function (template) {
        var score = compareMasks(mask, template);
        if (score > bestScore) {
          bestScore = score;
          bestDigit = digit;
        }
      });
    });

    return { digit: bestDigit, confidence: bestScore };
  }

  function compareMasks(a, b) {
    var intersection = 0;
    var union = 0;
    var same = 0;

    for (var i = 0; i < a.length; i += 1) {
      if (a[i] || b[i]) union += 1;
      if (a[i] && b[i]) intersection += 1;
      if (a[i] === b[i]) same += 1;
    }

    if (!union) return 0;
    return intersection / union * 0.75 + same / a.length * 0.25;
  }

  function recognizeInRegion(canvas, region) {
    var sourceWidth = canvas.width || canvas.clientWidth;
    var sourceHeight = canvas.height || canvas.clientHeight;
    if (!sourceWidth || !sourceHeight) return null;

    var sx = Math.max(0, Math.floor(sourceWidth * region.x));
    var sy = Math.max(0, Math.floor(sourceHeight * region.y));
    var sw = Math.min(sourceWidth - sx, Math.floor(sourceWidth * region.w));
    var sh = Math.min(sourceHeight - sy, Math.floor(sourceHeight * region.h));
    if (sw < 20 || sh < 20) return null;

    var scale = Math.min(1, 420 / sw);
    scratch.width = Math.max(1, Math.floor(sw * scale));
    scratch.height = Math.max(1, Math.floor(sh * scale));
    scratchContext.clearRect(0, 0, scratch.width, scratch.height);
    scratchContext.drawImage(canvas, sx, sy, sw, sh, 0, 0, scratch.width, scratch.height);

    var imageData = scratchContext.getImageData(0, 0, scratch.width, scratch.height);
    var threshold = thresholdImageData(imageData, false);
    var components = findComponents(threshold.mask, threshold.width, threshold.height)
      .sort(function (a, b) { return a.minX - b.minX; });

    var sequences = [];
    for (var i = 0; i < components.length; i += 1) {
      var base = components[i];
      var current = [base];
      var last = base;

      for (var j = i + 1; j < components.length; j += 1) {
        var next = components[j];
        var sameLine = Math.abs(next.cy - base.cy) <= Math.max(10, base.height * 0.45);
        var gap = next.minX - last.maxX;
        if (sameLine && gap >= -2 && gap <= Math.max(18, base.height * 0.65)) {
          current.push(next);
          last = next;
        }
      }

      if (current.length) sequences.push(current);
    }

    var best = null;
    sequences.forEach(function (sequence) {
      var digits = [];
      var confidences = [];

      sequence.forEach(function (box) {
        var raw = cropMask(threshold.mask, threshold.width, box);
        var normalized = normalizeMask(raw, box.width, box.height, TEMPLATE_WIDTH, TEMPLATE_HEIGHT);
        var match = matchDigit(normalized);
        if (match.confidence >= 0.34) {
          digits.push(match.digit);
          confidences.push(match.confidence);
        }
      });

      if (!digits.length) return;
      var value = parseInt(digits.join(""), 10);
      if (!Number.isFinite(value)) return;
      var confidence = confidences.reduce(function (sum, item) { return sum + item; }, 0) / confidences.length;
      var weighted = confidence + digits.length * 0.035 + (region.weight || 0);

      if (!best || weighted > best.weighted) {
        best = {
          value: value,
          confidence: confidence,
          weighted: weighted,
          digits: digits.join(""),
          region: region.name
        };
      }
    });

    return best;
  }

  function readCanvasScore() {
    var canvas = getGameCanvas();
    if (!canvas || !scratchContext) return;
    if (state.phase !== "running") return;

    var regions = [
      { name: "top-right", x: 0.54, y: 0.015, w: 0.43, h: 0.14, weight: 0.18 },
      { name: "top-center", x: 0.28, y: 0.015, w: 0.48, h: 0.14, weight: 0.06 },
      { name: "upper-right-wide", x: 0.45, y: 0.015, w: 0.52, h: 0.18, weight: 0.03 }
    ];

    var best = null;
    try {
      regions.forEach(function (region) {
        var result = recognizeInRegion(canvas, region);
        if (result && (!best || result.weighted > best.weighted)) {
          best = result;
        }
      });
    } catch (error) {
      state.scoreSource = "ocr-blocked";
      renderUI();
      return;
    }

    if (best && best.confidence >= 0.39) {
      setScore(best.value, "ocr:" + best.region);
    }
  }

  function startScoreReader() {
    if (readerTimer) return;
    readerTimer = window.setInterval(readCanvasScore, READ_INTERVAL_MS);
  }

  function canvasPoint(canvas, xRatio, yRatio) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: rect.left + rect.width * xRatio,
      y: rect.top + rect.height * yRatio
    };
  }

  function dispatchMouse(canvas, type, point) {
    canvas.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      clientX: point.x,
      clientY: point.y,
      button: 0
    }));
  }

  function dispatchKey(canvas, type, keyCode, key, code) {
    canvas.dispatchEvent(new KeyboardEvent(type, {
      bubbles: true,
      cancelable: true,
      keyCode: keyCode,
      which: keyCode,
      key: key,
      code: code
    }));
  }

  function restartGame() {
    var canvas = getGameCanvas();
    resetRun("restart");
    setPhase("running", "restart");

    if (!canvas) return false;
    canvas.focus && canvas.focus();

    var point = canvasPoint(canvas, 0.5, 0.72);
    ["mousedown", "mouseup", "click"].forEach(function (type) {
      dispatchMouse(canvas, type, point);
    });

    window.setTimeout(function () {
      dispatchKey(canvas, "keydown", 32, " ", "Space");
      dispatchKey(canvas, "keyup", 32, " ", "Space");
      dispatchKey(canvas, "keydown", 13, "Enter", "Enter");
      dispatchKey(canvas, "keyup", 13, "Enter", "Enter");
    }, 80);

    return true;
  }

  function install() {
    mountUI();
    installPokiHook();
    installUnityBridgeHook();
    startScoreReader();

    window.setTimeout(function () {
      if (state.phase === "loading" && getGameCanvas()) {
        setPhase("ready", "canvas");
      }
    }, 1500);
  }

  window.SubwayBridge = {
    getState: cloneState,
    restart: restartGame,
    setPhase: setPhase,
    setScore: function (score, source) {
      return setScore(score, source || "manual", { force: true });
    },
    pokiBridgeName: null
  };

  if (document.body) {
    install();
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }
})();
