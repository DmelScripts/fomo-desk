/* Hand-drawn charts. No chart library. */
(function (root) {
  "use strict";

  function fit(canvas) {
    var rect = canvas.getBoundingClientRect();
    var dpr = root.devicePixelRatio || 1;
    var w = Math.max(1, rect.width || canvas.clientWidth || 300);
    var h = Math.max(1, rect.height || canvas.clientHeight || 160);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function line(canvas, model) {
    var view = fit(canvas);
    var ctx = view.ctx;
    ctx.clearRect(0, 0, view.w, view.h);
    var points = (model && model.points) || [];
    var pad = { l: 8, r: 8, t: 28, b: 16 };
    if (!points.length) {
      ctx.save();
      ctx.strokeStyle = "rgba(235,235,245,0.3)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.l, view.h / 2);
      ctx.lineTo(view.w - pad.r, view.h / 2);
      ctx.stroke();
      ctx.restore();
      return;
    }
    var min = points[0].value;
    var max = points[0].value;
    points.forEach(function (point) {
      if (point.value < min) min = point.value;
      if (point.value > max) max = point.value;
    });
    if (typeof model.start === "number") {
      if (model.start < min) min = model.start;
      if (model.start > max) max = model.start;
    }
    if (min === max) {
      min -= 1;
      max += 1;
    }
    var t0 = points[0].ts;
    var t1 = points[points.length - 1].ts;
    if (t0 === t1) t1 = t0 + 1;
    function xOf(ts) {
      return pad.l + ((ts - t0) / (t1 - t0)) * (view.w - pad.l - pad.r);
    }
    function yOf(value) {
      return pad.t + (1 - (value - min) / (max - min)) * (view.h - pad.t - pad.b);
    }
    ctx.beginPath();
    points.forEach(function (point, index) {
      var x = xOf(point.ts);
      var y = yOf(point.value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(points[points.length - 1].ts), view.h - pad.b);
    ctx.lineTo(xOf(points[0].ts), view.h - pad.b);
    ctx.closePath();
    var gradient = ctx.createLinearGradient(0, pad.t, 0, view.h - pad.b);
    gradient.addColorStop(0, "rgba(0,211,90,0.28)");
    gradient.addColorStop(1, "rgba(0,211,90,0)");
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    points.forEach(function (point, index) {
      var x = xOf(point.ts);
      var y = yOf(point.value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#00d35a";
    ctx.lineWidth = 2;
    ctx.setLineDash([]);
    ctx.stroke();
    if (typeof model.start === "number") {
      var y = yOf(model.start);
      ctx.save();
      ctx.strokeStyle = "rgba(235,235,245,0.35)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(view.w - pad.r, y);
      ctx.stroke();
      ctx.restore();
    }
    var marks = (model && model.marks) || [];
    ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textBaseline = "top";
    var rows = [];
    marks.forEach(function (mark) {
      if (!mark || !mark.text || typeof mark.ts !== "number") return;
      var width = ctx.measureText(mark.text).width + 8;
      var x = Math.min(view.w - pad.r - width, Math.max(pad.l, xOf(mark.ts) - width / 2));
      var row = 0;
      while (rows[row] && rows[row].some(function (used) { return x < used.x + used.w && x + width > used.x; })) {
        row += 1;
      }
      if (!rows[row]) rows[row] = [];
      rows[row].push({ x: x, w: width });
      ctx.fillStyle = mark.color || "#fff";
      ctx.globalAlpha = model && model.motion ? 0.95 : 1;
      ctx.fillText(mark.text, x, 2 + row * 14);
      ctx.globalAlpha = 1;
    });
  }

  function candles(canvas, model) {
    var view = fit(canvas);
    var ctx = view.ctx;
    ctx.clearRect(0, 0, view.w, view.h);
    var rows = (model && model.candles) || [];
    if (!rows.length) return;
    var highs = rows.map(function (row) { return row.h; });
    var lows = rows.map(function (row) { return row.l; });
    var min = Math.min.apply(null, lows);
    var max = Math.max.apply(null, highs);
    if (model && typeof model.exitPrice === "number") {
      if (model.exitPrice < min) min = model.exitPrice;
      if (model.exitPrice > max) max = model.exitPrice;
    }
    if (min === max) {
      min -= 1;
      max += 1;
    }
    var volumeTop = view.h * 0.78;
    var pad = 8;
    var slot = (view.w - pad * 2) / rows.length;
    function yOf(price) {
      return 8 + (1 - (price - min) / (max - min)) * (volumeTop - 16);
    }
    var volumes = rows.map(function (row) { return typeof row.v === "number" ? row.v : null; });
    var volumeMax = null;
    volumes.forEach(function (value) {
      if (value == null) return;
      var size = Math.abs(value);
      if (volumeMax == null || size > volumeMax) volumeMax = size;
    });
    rows.forEach(function (row, index) {
      var x = pad + index * slot + slot / 2;
      var up = row.c >= row.o;
      ctx.strokeStyle = up ? "#00d35a" : "#ff453a";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yOf(row.h));
      ctx.lineTo(x, yOf(row.l));
      ctx.stroke();
      var top = yOf(Math.max(row.o, row.c));
      var height = Math.max(1, Math.abs(yOf(row.o) - yOf(row.c)));
      ctx.fillRect(x - Math.max(1, slot * 0.3), top, Math.max(2, slot * 0.6), height);
      if (volumes[index] != null && volumeMax) {
        var bar = (Math.abs(volumes[index]) / volumeMax) * (view.h - volumeTop - 8);
        ctx.globalAlpha = 0.45;
        ctx.fillRect(x - Math.max(1, slot * 0.3), view.h - 4 - bar, Math.max(2, slot * 0.6), bar);
        ctx.globalAlpha = 1;
      }
    });
    if (model && typeof model.exitPrice === "number") {
      var y = yOf(model.exitPrice);
      ctx.save();
      ctx.strokeStyle = "#ff9f0a";
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(pad, y);
      ctx.lineTo(view.w - pad, y);
      ctx.stroke();
      ctx.setLineDash([]);
      if (model.exitLabel) {
        ctx.fillStyle = "#ff9f0a";
        ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.fillText(model.exitLabel, pad, Math.max(0, y - 14));
      }
      ctx.restore();
    }
  }

  function ring(canvas, model) {
    var view = fit(canvas);
    var ctx = view.ctx;
    ctx.clearRect(0, 0, view.w, view.h);
    var cx = view.w / 2;
    var cy = view.h / 2;
    var radius = Math.min(view.w, view.h) / 2 - 6;
    ctx.lineWidth = 8;
    ctx.strokeStyle = "#2c2c2e";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();
    var score = model ? model.score : null;
    if (typeof score !== "number") return;
    var min = model && typeof model.min === "number" ? model.min : 70;
    ctx.strokeStyle = score >= min ? "#00d35a" : "#ffd60a";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(0, Math.min(100, score)) / 100);
    ctx.stroke();
    ctx.fillStyle = "#fff";
    ctx.font = "600 22px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(Math.round(score)), cx, cy);
  }

  root.DeskCharts = { line: line, candles: candles, ring: ring };
})(typeof globalThis !== "undefined" ? globalThis : this);
