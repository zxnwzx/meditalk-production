// 메디톡 — 공용 스크립트
// 인라인 onclick/onsubmit 대신 이벤트 위임 방식을 써서,
// 엄격한 Content-Security-Policy(script-src 'self')를 적용할 수 있게 합니다.

document.addEventListener("click", function (e) {
  const copyBtn = e.target.closest("[data-copy-link]");
  if (copyBtn) {
    const url = copyBtn.getAttribute("data-copy-link") || window.location.href;
    navigator.clipboard.writeText(url).then(function () {
      const original = copyBtn.textContent;
      copyBtn.textContent = "링크 복사됨";
      setTimeout(function () { copyBtn.textContent = original; }, 2000);
    });
    return;
  }
  const printBtn = e.target.closest("[data-print-btn]");
  if (printBtn) {
    window.print();
  }
});

document.addEventListener("submit", function (e) {
  const form = e.target.closest("[data-confirm]");
  if (!form) return;
  const msg = form.getAttribute("data-confirm") || "계속하시겠습니까?";
  if (!window.confirm(msg)) {
    e.preventDefault();
  }
});

// PWA: 서비스워커 등록 (지원 브라우저에서만, 실패해도 사이트 기능엔 영향 없음)
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/static/sw.js").catch(function () {});
  });
}

// ===================== 예약 발행 시각 프리셋 =====================
// 이 사이트는 사용자 브라우저의 시간대와 무관하게 항상 한국시간(KST)으로 예약을 해석합니다.
// 그래서 "지금"도 브라우저 로컬시간이 아니라, UTC에 9시간을 더한 KST 기준으로 계산합니다.
function _nowInKST() {
  return new Date(Date.now() + 9 * 60 * 60 * 1000);
}
function _formatForDatetimeLocal(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
document.addEventListener("click", function (e) {
  const btn = e.target.closest("[data-preset]");
  if (!btn) return;
  const input = document.getElementById("scheduled_at");
  if (!input) return;
  const preset = btn.getAttribute("data-preset");
  let d = _nowInKST();
  const oneDay = 24 * 60 * 60 * 1000;
  if (preset === "1h") {
    d = new Date(d.getTime() + 60 * 60 * 1000);
  } else if (preset === "3h") {
    d = new Date(d.getTime() + 3 * 60 * 60 * 1000);
  } else if (preset === "tonight") {
    const target = new Date(d.getTime());
    target.setUTCHours(18, 0, 0, 0);
    d = target.getTime() > d.getTime() ? target : new Date(target.getTime() + oneDay);
  } else if (preset === "tomorrow9") {
    d = new Date(d.getTime() + oneDay);
    d.setUTCHours(9, 0, 0, 0);
  } else if (preset === "tomorrow-morning") {
    d = new Date(d.getTime() + oneDay);
    d.setUTCHours(6, 0, 0, 0);
  }
  input.value = _formatForDatetimeLocal(d);
});

// ===================== 비밀번호 표시/숨기기 토글 =====================
document.addEventListener("click", function (e) {
  const btn = e.target.closest("[data-password-toggle]");
  if (!btn) return;
  const input = document.getElementById(btn.getAttribute("data-password-toggle"));
  if (!input) return;
  const wasHidden = input.type === "password";
  input.type = wasHidden ? "text" : "password";
  const nowVisible = wasHidden; // 원래 숨겨져 있었다면 토글 후엔 보이는 상태
  const openIcon = btn.querySelector(".icon-eye-open");
  const closedIcon = btn.querySelector(".icon-eye-closed");
  if (openIcon && closedIcon) {
    openIcon.hidden = nowVisible;
    closedIcon.hidden = !nowVisible;
  }
  btn.setAttribute("aria-label", nowVisible ? "비밀번호 숨기기" : "비밀번호 표시");
});

// 뉴스레터: 제출 후 페이지 전체가 초기화되지 않도록 폼 안에서 결과를 표시합니다.
document.addEventListener("submit", function (e) {
  const form = e.target.closest("[data-newsletter-form]");
  if (!form) return;
  e.preventDefault();
  const status = form.querySelector("[data-newsletter-status]");
  const submit = form.querySelector("button[type=submit]");
  const label = form.querySelector(".nl-submit-label");
  const loading = form.querySelector(".nl-submit-loading");
  if (submit) submit.disabled = true;
  if (label) label.hidden = true;
  if (loading) loading.hidden = false;
  if (status) { status.textContent = "구독 정보를 확인하고 있습니다…"; status.className = "newsletter-status is-loading"; }
  fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } })
    .then(function (response) { return response.json().then(function (data) { return { ok: response.ok && data.ok, message: data.message || "처리 중 오류가 발생했습니다." }; }); })
    .then(function (result) {
      if (status) { status.textContent = result.message; status.className = "newsletter-status " + (result.ok ? "is-success" : "is-error"); }
      if (result.ok) { form.querySelector("input[name=email]").value = ""; form.querySelectorAll("input[name=categories]").forEach(function (input) { input.checked = false; }); }
    })
    .catch(function () { if (status) { status.textContent = "잠시 후 다시 시도해 주세요."; status.className = "newsletter-status is-error"; } })
    .finally(function () { if (submit) submit.disabled = false; if (label) label.hidden = false; if (loading) loading.hidden = true; });
});

// 파일 선택 시 기자 프로필 사진 미리보기를 즉시 갱신합니다.
document.addEventListener("change", function (e) {
  if (e.target.id !== "avatar") return;
  const file = e.target.files && e.target.files[0];
  const preview = document.querySelector(".profile-photo-preview");
  if (!file || !preview) return;
  const reader = new FileReader();
  reader.onload = function () { preview.innerHTML = '<img src="' + reader.result + '" alt="선택한 프로필 사진 미리보기">'; };
    reader.readAsDataURL(file);
});

// 이미지 업로드: 파일 선택뿐 아니라 클립보드 이미지를 Ctrl/Cmd+V로 넣을 수 있습니다.
(function () {
  function imageInputs(scope) {
    return Array.from((scope || document).querySelectorAll('input[type="file"]')).filter(function (input) {
      return (input.accept || "").toLowerCase().indexOf("image") !== -1 && !input.disabled;
    });
  }
  function setFiles(input, files) {
    if (!input || !files.length || typeof DataTransfer === "undefined") return;
    var transfer = new DataTransfer();
    if (input.multiple && input.files) Array.from(input.files).forEach(function (file) { transfer.items.add(file); });
    files.forEach(function (file) { transfer.items.add(file); });
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function status(input, text, error) {
    var parent = input.parentElement;
    if (!parent) return;
    var node = parent.querySelector("[data-clipboard-status]");
    if (!node) { node = document.createElement("small"); node.setAttribute("data-clipboard-status", "true"); node.className = "clipboard-upload-status"; parent.appendChild(node); }
    node.textContent = text;
    node.classList.toggle("is-error", !!error);
  }
  function preview(input, file) {
    if (!file || !file.type || file.type.indexOf("image/") !== 0) return;
    var form = input.closest("form");
    var box = form && form.querySelector(".profile-photo-preview, .current-image-preview");
    if (!box) return;
    var reader = new FileReader();
    reader.onload = function () { box.innerHTML = '<img src="' + reader.result + '" alt="선택한 이미지 미리보기">'; };
    reader.readAsDataURL(file);
  }
  document.addEventListener("paste", function (event) {
    var items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    var files = Array.from(items).filter(function (item) { return item.kind === "file" && item.type.indexOf("image/") === 0; }).map(function (item) { return item.getAsFile(); }).filter(Boolean);
    if (!files.length) return;
    var active = document.activeElement;
    var input = active && active.matches && active.matches('input[type="file"]') ? active : null;
    if (!input) { var form = active && active.closest ? active.closest("form") : null; input = imageInputs(form)[0] || imageInputs(document)[0]; }
    if (!input) return;
    event.preventDefault();
    setFiles(input, files);
    preview(input, files[0]);
    status(input, files.length > 1 ? files.length + "개 이미지를 붙여넣었습니다." : "이미지를 붙여넣었습니다.", false);
  });
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches('input[type="file"]') || !input.files.length) return;
    preview(input, input.files[0]);
    status(input, input.files.length > 1 ? input.files.length + "개 이미지를 선택했습니다." : "이미지를 선택했습니다.", false);
  });
  document.addEventListener("DOMContentLoaded", function () {
    imageInputs(document).forEach(function (input) {
      if (input.parentElement.querySelector("[data-clipboard-hint]")) return;
      var hint = document.createElement("small");
      hint.setAttribute("data-clipboard-hint", "true");
      hint.className = "clipboard-upload-hint";
      hint.textContent = "이미지를 선택하거나 Ctrl/Cmd+V로 붙여넣으세요.";
      input.parentElement.appendChild(hint);
    });
  });
})();


