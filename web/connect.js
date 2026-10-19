// 连接帮助页：列出手机该打开的地址（带二维码）+ 游戏里怎么填 + 当前有没有收到数据。
(function () {
  "use strict";

  Forza.nav("connect");
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // 用 vendor/qrcode.js（qrcode-generator）生成 SVG 二维码；类型号 0 = 按内容长度自动选
  function qrSvg(text) {
    try {
      const qr = qrcode(0, "M");
      qr.addData(text);
      qr.make();
      return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true, alt: text });
    } catch (_) {
      return "";
    }
  }

  function isLoopback(host) {
    return host === "localhost" || host.startsWith("127.") || host === "[::1]";
  }

  function renderUrls(st) {
    const port = st.http_port || location.port || 80;
    const ips = [...(st.lan_ips || [])];
    // 当前就是用局域网地址打开的（比如手机上）：把它也列进去
    if (!isLoopback(location.hostname) && !ips.includes(location.hostname)) ips.unshift(location.hostname);
    const box = $("urls");
    if (!ips.length) {
      box.innerHTML = `<p class="warn" style="margin:0">没找到本机的局域网 IP：确认电脑已经连上 Wi-Fi 或网线。` +
        `也可以在命令行运行 <code>ipconfig</code>，找“IPv4 地址”（一般是 192.168.x.x），手机打开 http://那个IP:${esc(port)}/</p>`;
      return;
    }
    box.innerHTML = ips.map((ip) => {
      const url = `http://${ip}:${port}/`;
      const here = ip === location.hostname;
      return `<div class="url-item${here ? " here" : ""}">` +
        `<div class="qr">${qrSvg(url)}</div>` +
        `<a href="${esc(url)}">${esc(url)}</a>` +
        (here ? `<span class="tag">你现在打开的就是这个地址</span>` : "") +
        `</div>`;
    }).join("");
  }

  function renderGameTable(st) {
    const ip = (st.lan_ips || [])[0];
    for (const el of document.querySelectorAll(".lan-ip")) el.textContent = ip || "本机局域网 IP";
    for (const el of document.querySelectorAll(".udp-port")) el.textContent = st.udp_port;
    for (const el of document.querySelectorAll(".http-port")) el.textContent = st.http_port;
    $("fw-cmd").textContent = `New-NetFirewallRule -DisplayName "Forza Telemetry Web" -Direction Inbound ` +
      `-Protocol TCP -LocalPort ${st.http_port} -Action Allow -Profile Private`;
  }

  function renderState(st) {
    const age = st.since_last_packet;
    let dot = "", text;
    if (age === null || age === undefined) {
      text = `还没收到游戏数据（本程序在 UDP ${st.udp_port} 端口等着）`;
    } else if (age < 2) {
      dot = "on";
      text = `正在收到数据：${st.game_name || "未知游戏"} · ${st.format_label || ""} · ${Math.round(st.pps)} 包/秒`;
    } else {
      dot = "stale";
      text = `${Math.round(age)} 秒没收到数据了（游戏在菜单 / 暂停时会停发）`;
    }
    $("cn-state").innerHTML = `<span class="status-dot ${dot}"></span><span>${esc(text)}</span>`;
  }

  let first = true;
  async function refresh() {
    try {
      const st = await Forza.api("/api/status");
      if (first) {
        renderUrls(st);
        renderGameTable(st);
        first = false;
      }
      renderState(st);
    } catch (e) {
      $("cn-state").innerHTML = `<span class="status-dot"></span><span>连不上本程序的服务：${esc(e.message)}</span>`;
    }
  }
  refresh();
  setInterval(refresh, 3000);
})();
