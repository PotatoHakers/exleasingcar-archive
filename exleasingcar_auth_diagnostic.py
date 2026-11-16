"""Safe, one-run diagnostic for Exleasingcar's login flow.

This script reuses the project's Chromium profile and the existing keyring-based
login implementation. It does not print input values, credentials, cookies,
localStorage, sessionStorage, or full page HTML. No SQLite database is opened.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from exleasingcar_archive.collector import (
    AUCTION_LIST_URL,
    DEFAULT_PROFILE_DIR,
    ExleasingcarCollector,
    PlaywrightError,
    _wait_for_dynamic_content,
)


DIAGNOSTIC_SCRIPT = r"""() => {
  const isVisible = (el) => Boolean(
    el.getClientRects().length &&
    getComputedStyle(el).visibility !== "hidden" &&
    getComputedStyle(el).display !== "none"
  );
  const mask = (value) => String(value || "")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[EMAIL]")
    .replace(/\b\d{4,}\b/g, "[NUMBER]")
    .replace(/\b(?=[A-Z0-9]{8,}\b)(?=[A-Z0-9]*\d)[A-Z0-9]+\b/gi, "[ID]");
  const keywords = /войти|выйти|продолж|документ|загруз|уч[её]т|аккаунт|account|profile|log\s*out|sign\s*out|captcha|robot|робот|провер/i;
  const bodyLines = (document.body?.innerText || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => line && keywords.test(line))
    .slice(0, 35)
    .map((line) => [...line.matchAll(new RegExp(keywords.source, "gi"))]
      .map((match) => match[0]).join(", "));
  const visibleInputs = [...document.querySelectorAll("input")]
    .filter(isVisible)
    .map((el) => ({
      type: el.getAttribute("type"),
      name: mask(el.getAttribute("name")),
      id: mask(el.getAttribute("id")),
      placeholder: mask(el.getAttribute("placeholder")),
      autocomplete: el.getAttribute("autocomplete")
    }));
  const visibleControls = [...document.querySelectorAll("button, a, [role='button'], input[type='submit']")]
    .filter(isVisible)
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role"),
      matchingLabels: [...String(el.innerText || el.getAttribute("aria-label") || el.getAttribute("value") || "")
        .matchAll(new RegExp(keywords.source, "gi"))]
        .map((match) => match[0]),
      id: mask(el.getAttribute("id"))
    }))
    .filter((item) => item.matchingLabels.length > 0)
    .slice(0, 35);
  const body = document.body?.innerText || "";
  return {
    page: {
      origin: location.origin,
      path: location.pathname,
      title: mask(document.title)
    },
    signals: {
      passwordFieldVisible: [...document.querySelectorAll("input[type='password']")].some(isVisible),
      loginTextVisible: /войти|sign\s*in|log\s*in/i.test(body),
      logoutTextVisible: /выйти|log\s*out|sign\s*out/i.test(body),
      accountIdPhraseVisible: /ID\s+(?:вашей\s+)?уч[её]тной\s+записи/i.test(body),
      continueTextVisible: /продолжить/i.test(body),
      documentStepTextVisible: /загрузите?\s+(?:необходимые\s+)?документ|документ.{0,50}(?:загруз|удостовер)/i.test(body),
      captchaTextVisible: /captcha|recaptcha|не робот/i.test(body)
    },
    relevantBodyLines: bodyLines,
    visibleInputMetadataOnly: visibleInputs,
    relevantVisibleControls: visibleControls
  };
}"""


def main() -> None:
    collector = ExleasingcarCollector(
        database=None,  # Authentication diagnosis only; no database writes.
        raw_dir=Path("data/raw/collector"),
    )

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(DEFAULT_PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            print("Opening Exleasingcar with the project's persistent Chromium profile...")
            page.goto(AUCTION_LIST_URL, wait_until="domcontentloaded", timeout=60_000)
            _wait_for_dynamic_content(page)

            try:
                authenticated = collector._authenticate(
                    page,
                    reason="safe standalone authentication diagnostic",
                )
                print(f"AUTHENTICATION METHOD RESULT: {authenticated}")
            except (RuntimeError, PlaywrightError) as exc:
                print(f"AUTHENTICATION METHOD ERROR: {exc}")

            print("\nSAFE PAGE DIAGNOSTICS (no input values or session data):")
            result = page.evaluate(DIAGNOSTIC_SCRIPT)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            input(
                "\nПроверь открытое окно Chromium. Для завершения диагностики "
                "нажми Enter... "
            )
        finally:
            context.close()


if __name__ == "__main__":
    main()
