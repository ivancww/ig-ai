from ig_ai import reporting


def test_unified_report_preserves_codex_and_terminal_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "STATE_DIR", tmp_path / ".igai")
    monkeypatch.setattr(reporting, "UNIFIED_REPORT", tmp_path / "igai-report.txt")
    reporting.update_codex_report("PHASE 1 DISCOVERY HARDENING: PASS")
    reporting.update_terminal_report(
        command="ig-ai discover",
        checks="PASS",
        output="CST=do-not-show IG_API_KEY=do-not-show",
        secrets=("do-not-show",),
    )
    report = reporting.render_report()
    assert "=== CODEX / DEVELOPMENT REPORT ===" in report
    assert "PHASE 1 DISCOVERY HARDENING: PASS" in report
    assert "=== TERMINAL / RUNTIME REPORT ===" in report
    assert "do-not-show" not in report


def test_safe_text_redacts_secret_field_names():
    assert "[REDACTED SENSITIVE OUTPUT]" in reporting._safe_text("X-SECURITY-TOKEN=secret")
