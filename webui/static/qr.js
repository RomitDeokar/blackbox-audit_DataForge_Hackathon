/* Minimal dependency-free QR encoder (byte mode, ECC level L, versions 1-10).
 *
 * Written in-repo on purpose: the demo has to render a phone link on a
 * conference Wi-Fi that may not reach a CDN, and shipping a QR image endpoint
 * would mean a server round-trip for something that is pure arithmetic.
 *
 * Exposes: QRLite.render(container, text)  ->  draws a <canvas>
 */
(function (global) {
  'use strict';

  // ---- GF(256) with primitive polynomial 0x11d ----------------------------
  var EXP = new Uint8Array(512);
  var LOG = new Uint8Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
    for (var j = 255; j < 512; j++) EXP[j] = EXP[j - 255];
  })();

  function gmul(a, b) { return (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]]; }

  function rsGenPoly(n) {
    var p = [1];
    for (var i = 0; i < n; i++) {
      var np = new Array(p.length + 1);
      for (var k = 0; k < np.length; k++) np[k] = 0;
      for (var j = 0; j < p.length; j++) { np[j] ^= p[j]; np[j + 1] ^= gmul(p[j], EXP[i]); }
      p = np;
    }
    return p;
  }

  function rsEncode(data, ecLen) {
    var gen = rsGenPoly(ecLen);
    var res = new Array(ecLen);
    for (var i = 0; i < ecLen; i++) res[i] = 0;
    for (var d = 0; d < data.length; d++) {
      var factor = data[d] ^ res[0];
      res.shift(); res.push(0);
      for (var j = 0; j < ecLen; j++) res[j] ^= gmul(gen[j + 1], factor);
    }
    return res;
  }

  // ---- version tables (ECC level L only) ---------------------------------
  // [totalCodewords, ecPerBlock, blocksG1, dataPerBlockG1, blocksG2, dataPerBlockG2]
  var VER = {
    1:  [26,   7, 1,  19, 0,  0],
    2:  [44,  10, 1,  34, 0,  0],
    3:  [70,  15, 1,  55, 0,  0],
    4:  [100, 20, 1,  80, 0,  0],
    5:  [134, 26, 1, 108, 0,  0],
    6:  [172, 18, 2,  68, 0,  0],
    7:  [196, 20, 2,  78, 0,  0],
    8:  [242, 24, 2,  97, 0,  0],
    9:  [292, 30, 2, 116, 0,  0],
    10: [346, 18, 2,  68, 2, 69]
  };

  var ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]
  };

  function dataCodewords(v) {
    var t = VER[v];
    return t[3] * t[2] + t[5] * t[4];
  }

  function capacityBytes(v) {
    var countBits = v < 10 ? 8 : 16;
    return Math.floor((dataCodewords(v) * 8 - 4 - countBits) / 8);
  }

  function pickVersion(len) {
    for (var v = 1; v <= 10; v++) if (capacityBytes(v) >= len) return v;
    return 0;
  }

  // ---- bit stream --------------------------------------------------------
  function BitBuf() { this.bits = []; }
  BitBuf.prototype.put = function (value, length) {
    for (var i = length - 1; i >= 0; i--) this.bits.push((value >>> i) & 1);
  };

  function utf8Bytes(str) {
    var out = [];
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) { out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f)); }
      else if (c < 0xd800 || c >= 0xe000) { out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f)); }
      else {
        i++;
        var cp = 0x10000 + (((c & 0x3ff) << 10) | (str.charCodeAt(i) & 0x3ff));
        out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      }
    }
    return out;
  }

  // ---- BCH helpers -------------------------------------------------------
  function bchDigit(v) { var d = 0; while (v !== 0) { d++; v >>>= 1; } return d; }

  function formatBits(mask) {
    // ECC level L == 0b01
    var data = (1 << 3) | mask;
    var d = data << 10;
    while (bchDigit(d) - bchDigit(0x537) >= 0) d ^= 0x537 << (bchDigit(d) - bchDigit(0x537));
    return ((data << 10) | d) ^ 0x5412;
  }

  function versionBits(version) {
    var d = version << 12;
    while (bchDigit(d) - bchDigit(0x1f25) >= 0) d ^= 0x1f25 << (bchDigit(d) - bchDigit(0x1f25));
    return (version << 12) | d;
  }

  // ---- mask patterns -----------------------------------------------------
  function maskBit(mask, r, c) {
    switch (mask) {
      case 0: return (r + c) % 2 === 0;
      case 1: return r % 2 === 0;
      case 2: return c % 3 === 0;
      case 3: return (r + c) % 3 === 0;
      case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
      case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
      case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
      default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
    }
  }

  // ---- matrix ------------------------------------------------------------
  function buildBase(version) {
    var size = version * 4 + 17;
    var m = [], res = [], r, c;
    for (r = 0; r < size; r++) {
      m.push(new Array(size).fill(0));
      res.push(new Array(size).fill(false));
    }

    function finder(row, col) {
      for (var i = -1; i <= 7; i++) {
        for (var j = -1; j <= 7; j++) {
          var rr = row + i, cc = col + j;
          if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
          var on = (i >= 0 && i <= 6 && (j === 0 || j === 6)) ||
                   (j >= 0 && j <= 6 && (i === 0 || i === 6)) ||
                   (i >= 2 && i <= 4 && j >= 2 && j <= 4);
          m[rr][cc] = on ? 1 : 0;
          res[rr][cc] = true;
        }
      }
    }
    finder(0, 0); finder(0, size - 7); finder(size - 7, 0);

    // timing patterns
    for (var i = 8; i < size - 8; i++) {
      m[6][i] = i % 2 === 0 ? 1 : 0; res[6][i] = true;
      m[i][6] = i % 2 === 0 ? 1 : 0; res[i][6] = true;
    }

    // alignment patterns
    var centers = ALIGN[version];
    for (var a = 0; a < centers.length; a++) {
      for (var b = 0; b < centers.length; b++) {
        var ar = centers[a], ac = centers[b];
        if (res[ar][ac]) continue; // overlaps a finder
        for (var di = -2; di <= 2; di++) {
          for (var dj = -2; dj <= 2; dj++) {
            var on2 = Math.max(Math.abs(di), Math.abs(dj)) !== 1;
            m[ar + di][ac + dj] = on2 ? 1 : 0;
            res[ar + di][ac + dj] = true;
          }
        }
      }
    }

    // reserve format-info areas
    for (var k = 0; k <= 8; k++) {
      if (!res[8][k]) { res[8][k] = true; m[8][k] = 0; }
      if (!res[k][8]) { res[k][8] = true; m[k][8] = 0; }
    }
    for (var k2 = 0; k2 < 8; k2++) {
      res[8][size - 1 - k2] = true; m[8][size - 1 - k2] = 0;
      res[size - 1 - k2][8] = true; m[size - 1 - k2][8] = 0;
    }
    m[size - 8][8] = 1; res[size - 8][8] = true; // dark module

    // reserve + write version info (v7+)
    if (version >= 7) {
      var vb = versionBits(version);
      for (var p = 0; p < 18; p++) {
        var bit = (vb >>> p) & 1;
        var r1 = Math.floor(p / 3), c1 = size - 11 + (p % 3);
        m[r1][c1] = bit; res[r1][c1] = true;
        m[c1][r1] = bit; res[c1][r1] = true;
      }
    }

    return { size: size, m: m, res: res };
  }

  function penalty(m, size) {
    var score = 0, r, c, i;

    // Rule 1: runs of 5+ same-colour modules
    for (r = 0; r < size; r++) {
      for (var pass = 0; pass < 2; pass++) {
        var run = 1, prev = -1;
        for (i = 0; i < size; i++) {
          var val = pass === 0 ? m[r][i] : m[i][r];
          if (val === prev) { run++; if (run === 5) score += 3; else if (run > 5) score += 1; }
          else { run = 1; prev = val; }
        }
      }
    }

    // Rule 2: 2x2 blocks of the same colour
    for (r = 0; r < size - 1; r++) {
      for (c = 0; c < size - 1; c++) {
        var s = m[r][c] + m[r][c + 1] + m[r + 1][c] + m[r + 1][c + 1];
        if (s === 0 || s === 4) score += 3;
      }
    }

    // Rule 3: finder-like 1:1:3:1:1 patterns
    var pat = [1, 0, 1, 1, 1, 0, 1];
    function matches(get, start) {
      for (var k = 0; k < 7; k++) if (get(start + k) !== pat[k]) return false;
      return true;
    }
    for (r = 0; r < size; r++) {
      for (c = 0; c + 7 <= size; c++) {
        var rowGet = function (x) { return m[r][x]; };
        var colGet = function (x) { return m[x][r]; };
        if (matches(rowGet, c)) score += 40;
        if (matches(colGet, c)) score += 40;
      }
    }

    // Rule 4: deviation from 50% dark
    var dark = 0;
    for (r = 0; r < size; r++) for (c = 0; c < size; c++) dark += m[r][c];
    var pct = (dark * 100) / (size * size);
    score += Math.floor(Math.abs(pct - 50) / 5) * 10;

    return score;
  }

  function encode(text) {
    var bytes = utf8Bytes(text);
    var version = pickVersion(bytes.length);
    if (!version) throw new Error('QRLite: payload too long (' + bytes.length + ' bytes, max ' + capacityBytes(10) + ')');

    var t = VER[version];
    var ecPerBlock = t[1], g1 = t[2], d1 = t[3], g2 = t[4], d2 = t[5];
    var totalData = dataCodewords(version);

    // build the bit stream: mode 0100 (byte) + length + payload
    var buf = new BitBuf();
    buf.put(4, 4);
    buf.put(bytes.length, version < 10 ? 8 : 16);
    for (var i = 0; i < bytes.length; i++) buf.put(bytes[i], 8);

    // terminator + pad to byte boundary
    var capBits = totalData * 8;
    var termi = Math.min(4, capBits - buf.bits.length);
    buf.put(0, termi);
    while (buf.bits.length % 8 !== 0) buf.bits.push(0);

    var cw = [];
    for (var b = 0; b < buf.bits.length; b += 8) {
      var byteVal = 0;
      for (var k = 0; k < 8; k++) byteVal = (byteVal << 1) | buf.bits[b + k];
      cw.push(byteVal);
    }
    // pad codewords alternate 0xEC / 0x11
    var padToggle = true;
    while (cw.length < totalData) { cw.push(padToggle ? 0xec : 0x11); padToggle = !padToggle; }

    // split into blocks, compute EC, interleave
    var blocks = [], pos = 0, n;
    for (n = 0; n < g1; n++) { blocks.push(cw.slice(pos, pos + d1)); pos += d1; }
    for (n = 0; n < g2; n++) { blocks.push(cw.slice(pos, pos + d2)); pos += d2; }
    var ecBlocks = blocks.map(function (blk) { return rsEncode(blk, ecPerBlock); });

    var interleaved = [];
    var maxData = Math.max(d1, d2 || 0);
    for (i = 0; i < maxData; i++) {
      for (n = 0; n < blocks.length; n++) if (i < blocks[n].length) interleaved.push(blocks[n][i]);
    }
    for (i = 0; i < ecPerBlock; i++) {
      for (n = 0; n < ecBlocks.length; n++) interleaved.push(ecBlocks[n][i]);
    }

    var bitStream = [];
    for (i = 0; i < interleaved.length; i++) {
      for (k = 7; k >= 0; k--) bitStream.push((interleaved[i] >>> k) & 1);
    }

    // try every mask, keep the lowest penalty
    var best = null;
    for (var mask = 0; mask < 8; mask++) {
      var base = buildBase(version);
      var size = base.size, m = base.m, res = base.res;

      // data placement, right to left in column pairs
      var dir = -1, row = size - 1, col = size - 1, bitIdx = 0;
      while (col > 0) {
        if (col === 6) col--;
        for (;;) {
          for (var cOff = 0; cOff < 2; cOff++) {
            var cc = col - cOff;
            if (res[row][cc]) continue;
            var bit = bitIdx < bitStream.length ? bitStream[bitIdx] : 0;
            bitIdx++;
            m[row][cc] = maskBit(mask, row, cc) ? bit ^ 1 : bit;
          }
          row += dir;
          if (row < 0 || row >= size) { row -= dir; dir = -dir; break; }
        }
        col -= 2;
      }

      // format info
      var fb = formatBits(mask);
      for (i = 0; i < 15; i++) {
        var fbit = (fb >>> i) & 1;
        // vertical strip beside the top-left finder
        if (i < 6) m[i][8] = fbit;
        else if (i < 8) m[i + 1][8] = fbit;
        else if (i === 8) m[8][7] = fbit;
        else m[8][14 - i] = fbit;
        // duplicated copy
        if (i < 8) m[8][size - 1 - i] = fbit;
        else m[size - 15 + i][8] = fbit;
      }

      var score = penalty(m, size);
      if (best === null || score < best.score) best = { score: score, m: m, size: size };
    }

    return { size: best.size, modules: best.m, version: version };
  }

  function render(container, text, opts) {
    opts = opts || {};
    var scale = opts.scale || 4;
    var quiet = opts.quiet == null ? 4 : opts.quiet;
    var qr = encode(text);
    var dim = (qr.size + quiet * 2) * scale;

    var canvas = document.createElement('canvas');
    var dpr = global.devicePixelRatio || 1;
    canvas.width = dim * dpr;
    canvas.height = dim * dpr;
    canvas.style.width = dim + 'px';
    canvas.style.height = dim + 'px';

    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, dim, dim);
    ctx.fillStyle = '#000000';
    for (var r = 0; r < qr.size; r++) {
      for (var c = 0; c < qr.size; c++) {
        if (qr.modules[r][c]) {
          ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
        }
      }
    }

    container.innerHTML = '';
    container.appendChild(canvas);
    return qr;
  }

  global.QRLite = { encode: encode, render: render, capacityBytes: capacityBytes };
})(window);
