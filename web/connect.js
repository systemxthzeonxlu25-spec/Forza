// 连接帮助页：列出手机该打开的地址（带二维码）+ 游戏里怎么填 + 当前有没有收到数据。
(function () {
  "use strict";

  Forza.nav("connect");
  const $ = (id) => document.getElementById(id);
  const esc = Forza.esc;      // 公用的 HTML 转义（见 common.js）

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
    // 本机打开时 /api/status 会给出局域网修改钥匙：二维码网址带上它，手机扫一次就能改数据
    const key = st.access_key ? `?key=${encodeURIComponent(st.access_key)}` : "";
    box.innerHTML = ips.map((ip) => {
      const shown = `http://${ip}:${port}/`;
      const url = shown + key;           // 二维码和链接带钥匙，显示的文字不带（太长，也免得被截图带走）
      const here = ip === location.hostname;
      return `<div class="url-item${here ? " here" : ""}">` +
        `<div class="qr">${qrSvg(url)}</div>` +
        `<a href="${esc(url)}">${esc(shown)}</a>` +
        (here ? `<span class="tag">你现在打开的就是这个地址</span>` : "") +
        `</div>`;
    }).join("") + accessNote(st);
  }

  function accessNote(st) {
    if (st.lan_access !== "key") return "";
    if (st.access_key) {
      return `<p class="muted" style="flex-basis:100%;margin:0">二维码里带着修改钥匙：用它打开过的手机可以删圈、改名、开关录制；` +
        `只手动输入地址的设备只能看。钥匙存在数据库旁边的 access_key.txt，删掉它重启就换一把新的。</p>`;
    }
    if (st.can_write === false) {
      return `<p class="warn" style="flex-basis:100%;margin:0">这台设备现在只能看、不能改数据：在电脑上打开这一页，用这台设备扫一次二维码就行。</p>`;
    }
    return "";
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
