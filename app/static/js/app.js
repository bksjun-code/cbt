// 응시 화면 보조 스크립트. 타이머는 표시용이며 만료 판정은 항상 서버가 한다.
// 우클릭/복사 방지는 보조 수단일 뿐, 핵심 보호는 서버 측 정답 비노출이다.
(function () {
  "use strict";

  var deadline = null; // epoch ms
  var timerEl = null;
  var submitting = false;

  function fmt(sec) {
    var m = Math.floor(sec / 60), s = sec % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function tick() {
    if (!timerEl || deadline === null) return;
    var left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    timerEl.textContent = fmt(left);
    timerEl.classList.toggle("warn", left <= 300);
    if (left <= 0 && !submitting) {
      submitting = true;
      var f = document.getElementById("submit-form");
      if (f) f.submit(); // 서버가 만료 여부를 판단해 처리
    }
  }

  function syncFrom(el) {
    var r = parseInt(el && el.getAttribute("data-remaining"), 10);
    if (!isNaN(r)) deadline = Date.now() + r * 1000; // 서버 기준 남은 시간으로 보정
  }

  document.addEventListener("DOMContentLoaded", function () {
    timerEl = document.getElementById("timer");
    if (timerEl) {
      syncFrom(timerEl);
      tick();
      setInterval(tick, 1000);
    }

    var sf = document.getElementById("submit-form");
    var dlg = document.getElementById("submit-dialog");

    function setLine(id, text) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      el.hidden = !text;
    }

    function finalSubmit() {
      submitting = true;
      sf.submit(); // form.submit() 은 submit 이벤트를 다시 발생시키지 않는다
    }

    if (sf) {
      sf.addEventListener("submit", function (e) {
        if (submitting) return; // 시간 종료 자동 제출은 확인 없이
        e.preventDefault();
        var nav = document.getElementById("navigator");
        var unanswered = nav ? parseInt(nav.getAttribute("data-unanswered"), 10) || 0 : 0;
        var flagged = document.querySelectorAll(".nav-item.flagged").length;

        if (!dlg || typeof dlg.showModal !== "function") { // <dialog> 미지원 브라우저: 기본 확인창으로 대체
          var msg = (unanswered > 0 ? "미응답 " + unanswered + "문항이 있습니다.\n" : "") + "최종 제출하면 수정할 수 없습니다. 제출할까요?";
          if (window.confirm(msg)) finalSubmit();
          return;
        }
        setLine("submit-dialog-unanswered", unanswered > 0 ? "미응답 " + unanswered + "문항이 있습니다." : "");
        setLine("submit-dialog-flagged", flagged > 0 ? "검토 표시한 문항이 " + flagged + "개 있습니다." : "");
        dlg.returnValue = "";
        dlg.showModal(); // 화면 가운데 표시, 뒤 화면은 어둡게 가려지고 Esc 로 취소된다
      });
    }
    if (dlg) {
      dlg.addEventListener("close", function () {
        if (dlg.returnValue === "confirm" && !submitting) finalSubmit();
      });
    }
  });

  // 저장 응답마다 서버 남은 시간으로 타이머 보정
  document.addEventListener("htmx:afterSwap", function () {
    syncFrom(document.getElementById("save-status"));
  });
  document.addEventListener("htmx:oobAfterSwap", function () {
    syncFrom(document.getElementById("save-status"));
  });

  // 선택 해제 버튼
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-clear]");
    if (!b) return;
    var form = b.closest("form");
    var changed = false;
    form.querySelectorAll("input[name=selected]:checked").forEach(function (i) { i.checked = false; changed = true; });
    if (changed) form.dispatchEvent(new Event("change", { bubbles: true }));
  });

  // 보조 방지: 응시 영역 우클릭/복사/잘라내기/드래그 선택
  ["contextmenu", "copy", "cut", "dragstart"].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      if (e.target.closest && e.target.closest(".exam-body")) e.preventDefault();
    });
  });
})();
