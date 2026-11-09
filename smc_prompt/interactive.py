"""Terminal Interactive Wizard for smc-prompt-cli.

Provides a guided, step-by-step interactive CLI interface for users
who prefer a conversational prompt instead of remembering one-liner flags.
Includes smart defaults so common configurations (like timeframes) can
be selected with a single ENTER keystroke.
"""

from __future__ import annotations

import os
import sys

import click

from . import config as cfg
from .errors import SmcPromptError, exit_code_for


def _banner() -> None:
    """Print the interactive wizard header banner."""
    click.echo("")
    click.secho("=" * 70, fg="cyan", bold=True)
    click.secho("  SMC-PROMPT CLI - INTERACTIVE WIZARD", fg="bright_white", bold=True)
    click.secho("  Generator Payload Fakta SMC/ICT & Analisis Multi-Timeframe", fg="cyan")
    click.secho("=" * 70, fg="cyan", bold=True)
    click.echo("")


def _prompt_choice(
    title: str,
    options: list[tuple[str, str]],
    default_index: int = 0,
) -> str:
    """Prompt user to select one option from a numbered list."""
    click.secho(title, fg="yellow", bold=True)
    for idx, (label, desc) in enumerate(options, 1):
        suffix = click.style(" [Default]", fg="green", bold=True) if idx - 1 == default_index else ""
        click.echo(f"  {click.style(str(idx), fg='cyan', bold=True)}. {label} - {desc}{suffix}")

    while True:
        default_val = str(default_index + 1)
        raw = click.prompt(
            click.style(f"Pilihan Anda [1-{len(options)}]", fg="bright_white"),
            default=default_val,
            show_default=True,
        ).strip()

        try:
            choice_num = int(raw)
            if 1 <= choice_num <= len(options):
                return options[choice_num - 1][0]
        except ValueError:
            pass
        click.secho(f"Input tidak valid. Masukkan angka antara 1 dan {len(options)}.", fg="red")


def _parse_dual_float_input(raw: str) -> tuple[float | None, float | None]:
    """Parse string input harga yang bisa berupa nilai tunggal ('65000') atau pasangan ('65000, 64800')."""
    cleaned = raw.strip()
    if not cleaned:
        return None, None
    parts = [p.strip() for p in cleaned.replace("/", ",").split(",") if p.strip()]
    if not parts:
        return None, None
    try:
        val1 = float(parts[0])
    except ValueError:
        return None, None
    val2: float | None = None
    if len(parts) > 1:
        try:
            val2 = float(parts[1])
        except ValueError:
            val2 = None
    return val1, val2


def format_cli_command(
    symbol: str,
    *,
    provider: str = cfg.PROVIDER_BINANCE,
    input_csv: str | None = None,
    twelvedata_key: str | None = None,
    oanda_token: str | None = None,
    action: str = "prompt",
    preset: str | None = None,
    htf_interval: str = cfg.HTF_INTERVAL,
    mtf_interval: str = cfg.MTF_INTERVAL,
    ltf_interval: str = cfg.LTF_INTERVAL,
    swing_lookback: int = cfg.DEFAULT_SWING_LOOKBACK,
    candles_only: bool = False,
    review_interval: str = cfg.DEFAULT_REVIEW_INTERVAL,
    review_candles: int = cfg.DEFAULT_REVIEW_CANDLES,
    review_direction: str | None = None,
    review_entry: float | None = None,
    review_entry2: float | None = None,
    review_sl: float | None = None,
    review_tp: float | None = None,
    review_tp2: float | None = None,
    review_outcome: str | None = None,
    review_exit: float | None = None,
    review_notes: str | None = None,
    validate_setup: bool = False,
    entry_price: float | None = None,
    entry2_price: float | None = None,
    tp_price: float | None = None,
    tp2_price: float | None = None,
    sl_price: float | None = None,
    order_status: str | None = None,
    validate_interval: str = cfg.DEFAULT_VALIDATE_INTERVAL,
    validate_candles: int = cfg.DEFAULT_VALIDATE_CANDLES,
    print_stdout: bool = False,
) -> str:
    """Rakit perintah CLI satu baris (one-liner) smc-prompt murni dari kumpulan konfigurasi."""
    cli_parts = [f"smc-prompt {symbol.upper()}"]
    if provider != cfg.PROVIDER_BINANCE and provider != "csv":
        cli_parts.append(f"--provider {provider}")
    if provider == "csv" and input_csv:
        cli_parts.append(f"--input-csv {input_csv}")
    if twelvedata_key:
        cli_parts.append(f"--twelvedata-key {twelvedata_key}")
    if oanda_token:
        cli_parts.append(f"--oanda-token {oanda_token}")

    is_review = action == "review" or candles_only
    is_validate = action == "validate" or validate_setup

    if is_review:
        cli_parts.append("--candles-only")
        if review_interval != cfg.DEFAULT_REVIEW_INTERVAL:
            cli_parts.append(f"--review-interval {review_interval}")
        if review_candles != cfg.DEFAULT_REVIEW_CANDLES:
            cli_parts.append(f"--review-candles {review_candles}")
        if review_direction:
            cli_parts.append(f"--direction {review_direction}")
        if review_entry is not None:
            cli_parts.append(f"--entry {review_entry}")
        if review_entry2 is not None:
            cli_parts.append(f"--entry2 {review_entry2}")
        if review_sl is not None:
            cli_parts.append(f"--sl {review_sl}")
        if review_tp is not None:
            cli_parts.append(f"--tp {review_tp}")
        if review_tp2 is not None:
            cli_parts.append(f"--tp2 {review_tp2}")
        if review_outcome:
            cli_parts.append(f'--outcome "{review_outcome}"')
        if review_exit is not None:
            cli_parts.append(f"--exit {review_exit}")
        if review_notes:
            clean_notes = review_notes.replace('"', '\\"')
            cli_parts.append(f'--notes "{clean_notes}"')
    elif is_validate:
        cli_parts.append("--validate-setup")
        if entry_price is not None:
            cli_parts.append(f"--entry {entry_price}")
        if entry2_price is not None:
            cli_parts.append(f"--entry2 {entry2_price}")
        if tp_price is not None:
            cli_parts.append(f"--tp {tp_price}")
        if tp2_price is not None:
            cli_parts.append(f"--tp2 {tp2_price}")
        if sl_price is not None:
            cli_parts.append(f"--sl {sl_price}")
        if order_status == "unfilled":
            cli_parts.append("--unfilled")
        elif order_status == "partial":
            cli_parts.append("--partial")
        elif order_status == "filled":
            cli_parts.append("--filled")
        elif order_status:
            cli_parts.append(f"--order-status {order_status}")
        if validate_interval != cfg.DEFAULT_VALIDATE_INTERVAL:
            cli_parts.append(f"--validate-interval {validate_interval}")
        if validate_candles != cfg.DEFAULT_VALIDATE_CANDLES:
            cli_parts.append(f"--validate-candles {validate_candles}")
    else:
        if preset and preset != cfg.DEFAULT_PRESET:
            cli_parts.append(f"--preset {preset}")
            p_htf, p_mtf, p_ltf = cfg.TIMEFRAME_PRESETS[preset]
            if htf_interval != p_htf:
                cli_parts.append(f"--htf-interval {htf_interval}")
            if mtf_interval != p_mtf:
                cli_parts.append(f"--mtf-interval {mtf_interval}")
            if ltf_interval != p_ltf:
                cli_parts.append(f"--ltf-interval {ltf_interval}")
        else:
            if htf_interval != cfg.HTF_INTERVAL:
                cli_parts.append(f"--htf-interval {htf_interval}")
            if mtf_interval != cfg.MTF_INTERVAL:
                cli_parts.append(f"--mtf-interval {mtf_interval}")
            if ltf_interval != cfg.LTF_INTERVAL:
                cli_parts.append(f"--ltf-interval {ltf_interval}")
        if swing_lookback != cfg.DEFAULT_SWING_LOOKBACK:
            cli_parts.append(f"--swing-lookback {swing_lookback}")

    if print_stdout:
        cli_parts.append("--stdout")

    return " ".join(cli_parts)


def run_interactive_wizard() -> None:
    """Run the guided interactive CLI wizard and execute the analysis."""
    _ensure_clean_exit = True
    try:
        _banner()

        # Step 1: Pilih Sumber Data (Data Provider)
        provider_options = [
            (cfg.PROVIDER_BINANCE, "Binance Futures (Crypto USDT-M, otomatis fallback jika terblokir)"),
            (cfg.PROVIDER_BITUNIX, "Bitunix Futures (Crypto USDT-M, tanpa API key)"),
            (cfg.PROVIDER_MEXC, "MEXC Futures (Crypto USDT-M, tanpa API key)"),
            (cfg.PROVIDER_GATE, "Gate.io Futures (Crypto USDT-M, tanpa API key)"),
            (cfg.PROVIDER_TWELVEDATA, "Twelve Data (Spot Forex & Logam Mulia seperti XAUUSD)"),
            (cfg.PROVIDER_OANDA, "OANDA (Spot Forex & Logam Mulia seperti XAUUSD)"),
            ("csv", "Offline CSV (Membaca file data lokal di komputer)"),
        ]
        provider = _prompt_choice(
            "[Langkah 1/5] Pilih Sumber Data (Data Provider):",
            provider_options,
            default_index=0,
        )
        click.echo("")

        # Step 2: Simbol / Ticker
        default_symbol = "BTCUSDT"
        symbol_hint = "Contoh: BTCUSDT, ETHUSDT, SOLUSDT"
        if provider in (cfg.PROVIDER_TWELVEDATA, cfg.PROVIDER_OANDA):
            default_symbol = "XAUUSD"
            symbol_hint = "Contoh: XAUUSD, EURUSD, GBPUSD"

        input_csv: str | None = None
        if provider == "csv":
            click.secho("[Langkah 2/5] Tentukan Berkas CSV Lokal:", fg="yellow", bold=True)
            input_csv = click.prompt(
                click.style("Path file CSV (kolom: open_time,open,high,low,close,volume)", fg="bright_white"),
                type=click.Path(exists=True, dir_okay=False),
            ).strip()
            symbol = click.prompt(
                click.style("Label Simbol Aset", fg="bright_white"),
                default="BTCUSDT",
            ).strip().upper()
        else:
            click.secho(f"[Langkah 2/5] Masukkan Ticker / Simbol ({symbol_hint}):", fg="yellow", bold=True)
            symbol = click.prompt(
                click.style("Ticker Simbol", fg="bright_white"),
                default=default_symbol,
            ).strip().upper()
        click.echo("")

        # Validasi kredensial provider jika diperlukan (Twelve Data / OANDA)
        twelvedata_key: str | None = None
        oanda_token: str | None = None
        oanda_account_id: str | None = None
        oanda_env: str = cfg.OANDA_ENV_PRACTICE

        if provider == cfg.PROVIDER_TWELVEDATA:
            existing_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
            if not existing_key:
                click.secho(
                    "  [!] TWELVEDATA_API_KEY tidak terdeteksi di .env maupun shell environment.",
                    fg="yellow",
                    bold=True,
                )
                twelvedata_key = click.prompt(
                    click.style("  Masukkan Twelve Data API Key", fg="bright_white"),
                    type=str,
                ).strip()

        elif provider == cfg.PROVIDER_OANDA:
            existing_token = os.environ.get("OANDA_API_TOKEN", "").strip()
            if not existing_token:
                click.secho(
                    "  [!] OANDA_API_TOKEN tidak terdeteksi di .env maupun shell environment.",
                    fg="yellow",
                    bold=True,
                )
                oanda_token = click.prompt(
                    click.style("  Masukkan OANDA API Token", fg="bright_white"),
                    type=str,
                ).strip()
            oanda_account_id = os.environ.get("OANDA_ACCOUNT_ID", "").strip() or None

        # Step 3: Mode Tindakan / Mau Ngapain
        action_options = [
            ("prompt", "Generate Prompt SMC/ICT (Trend, FVG, Liquidity Swings untuk LLM)"),
            ("review", "Review Pasca-Trade / Ekspor Candlestick Mentah (--candles-only)"),
            ("validate", "Validasi Setup / Checklist Sebelum Entry (--validate-setup)"),
        ]
        action = _prompt_choice(
            "[Langkah 3/5] Mau ngapain? (Pilih Mode Tindakan):",
            action_options,
            default_index=0,
        )
        click.echo("")

        # Step 4: Timeframe & Parameter (Smart Defaults)
        htf_interval = cfg.HTF_INTERVAL
        mtf_interval = cfg.MTF_INTERVAL
        ltf_interval = cfg.LTF_INTERVAL
        htf_candles = cfg.DEFAULT_HTF_CANDLES
        mtf_candles = cfg.DEFAULT_MTF_CANDLES
        ltf_candles = cfg.DEFAULT_LTF_CANDLES
        swing_lookback = cfg.DEFAULT_SWING_LOOKBACK

        candles_only = False
        review_interval = cfg.DEFAULT_REVIEW_INTERVAL
        review_candles = cfg.DEFAULT_REVIEW_CANDLES
        review_direction: str | None = None
        review_entry: float | None = None
        review_entry2: float | None = None
        review_sl: float | None = None
        review_tp: float | None = None
        review_tp2: float | None = None
        review_outcome: str | None = None
        review_notes: str | None = None
        review_exit: float | None = None     # Level Exit / Harga Hit riil

        validate_setup = False
        entry_price: float | None = None
        entry2_price: float | None = None
        tp_price: float | None = None
        tp2_price: float | None = None
        sl_price: float | None = None
        order_status: str | None = None
        validate_interval = cfg.DEFAULT_VALIDATE_INTERVAL
        validate_candles = cfg.DEFAULT_VALIDATE_CANDLES

        selected_preset: str | None = cfg.DEFAULT_PRESET

        if action == "prompt":
            click.secho("[Langkah 4/5] Pengaturan Timeframe & Template Gaya Trading:", fg="yellow", bold=True)
            preset_options = [
                ("intraday", "Intraday Konservatif (1d / 1h / 15m) - Target PDH/PDL, filter noise"),
                ("sniper", "Intraday Sniper / Killzone (4h / 1h / 5m) - Cepat tanggap 4H, entry presisi 5m tight SL"),
                ("balanced", "Intraday Seimbang (4h / 1h / 15m) - Rasio harmonis 4x fraktal"),
                ("swing", "Swing Trading (1d / 4h / 1h) - Trend makro beberapa hari s.d. minggu"),
                ("scalp", "Scalping Cepat (1h / 15m / 5m) - Eksekusi menit, session extremes"),
                ("custom", "Kustom Timeframe - Tentukan HTF, MTF, dan LTF sendiri"),
            ]
            chosen_style = _prompt_choice("Pilih Template Gaya Trading:", preset_options, default_index=0)
            if chosen_style != "custom":
                selected_preset = chosen_style
                htf_interval, mtf_interval, ltf_interval = cfg.TIMEFRAME_PRESETS[chosen_style]
                click.echo(f"  * Template aktif: {click.style(chosen_style.upper(), fg='cyan', bold=True)} ({htf_interval} / {mtf_interval} / {ltf_interval})")
            else:
                selected_preset = None
                htf_interval = click.prompt("HTF interval (e.g. 1d, 4h)", default=htf_interval)
                mtf_interval = click.prompt("MTF interval (e.g. 4h, 2h)", default=mtf_interval)
                ltf_interval = click.prompt("LTF interval (e.g. 1h, 15m)", default=ltf_interval)
                swing_lookback = int(click.prompt("Swing lookback N (ganjil >= 3)", type=int, default=swing_lookback))

        elif action == "review":
            candles_only = True
            click.secho("[Langkah 4/5] Pengaturan Review Pasca-Trade:", fg="yellow", bold=True)
            click.echo(f"  Setelan default: Interval {click.style(review_interval, fg='cyan')}, Jumlah {click.style(str(review_candles), fg='cyan')} candle")
            use_default_review = click.confirm(
                click.style("Gunakan setelan default di atas?", fg="bright_white"),
                default=True,
            )
            if not use_default_review:
                review_interval = click.prompt("Interval candle (e.g. 1h, 15m, 4h)", default=review_interval)
                review_candles = click.prompt("Jumlah closed candle yang diekspor", type=int, default=review_candles)

            click.echo("")
            fill_journal = click.confirm(
                click.style("Ingin melengkapi jurnal & evaluasi trade sekarang? (Tekan Enter jika Ya)", fg="bright_white", bold=True),
                default=True,
            )
            if fill_journal:
                click.secho("  --- Input Data Jurnal & Evaluasi Trade ---", fg="cyan")
                dir_opts = [
                    ("long", "Long (Beli / Naik)"),
                    ("short", "Short (Jual / Turun)"),
                ]
                review_direction = _prompt_choice("Arah Posisi:", dir_opts, default_index=0)

                entry_input = click.prompt(
                    "Level Entry (harga masuk, kosongkan jika belum ada)",
                    default="",
                    show_default=False,
                ).strip()
                review_entry, review_entry2 = _parse_dual_float_input(entry_input)

                sl_input = click.prompt(
                    "Stop Loss (SL) (opsional, kosongkan jika belum ada)",
                    default="",
                    show_default=False,
                ).strip()
                review_sl, _ = _parse_dual_float_input(sl_input)

                tp_input = click.prompt(
                    "Take Profit (TP) (opsional, kosongkan jika belum ada)",
                    default="",
                    show_default=False,
                ).strip()
                review_tp, review_tp2 = _parse_dual_float_input(tp_input)

                # Verifikasi konsistensi trade plan secara proaktif jika data esensial diisi
                if review_entry is not None and (review_sl is not None or review_tp is not None):
                    try:
                        from .trade_plan import build_trade_plan
                        build_trade_plan(
                            entry1=review_entry,
                            entry2=review_entry2,
                            tp1=review_tp,
                            tp2=review_tp2,
                            sl=review_sl,
                            direction=review_direction,
                        )
                    except SmcPromptError as err:
                        click.secho(f"  [!] Perhatian susunan setup: {err}", fg="yellow")

                outcome_opts = [
                    ("Hit TP", "Hit TP (Target Profit Tercapai)"),
                    ("Hit SL", "Hit SL (Stop Loss Kena)"),
                    ("BE", "BE (Break-Even / Impas)"),
                    ("Cut Manual", "Cut Manual (Ditutup Manual Sebelum TP/SL)"),
                ]
                review_outcome = _prompt_choice("Hasil Akhir Trade:", outcome_opts, default_index=0)

                # Prompt Level Exit / Harga Hit dengan smart default berbasis outcome
                exit_default = ""
                exit_default_hint = "kosongkan jika belum ada"
                if review_outcome == "Hit TP" and review_tp is not None:
                    exit_default = str(review_tp)
                    exit_default_hint = f"default TP = {review_tp}"
                elif review_outcome == "Hit SL" and review_sl is not None:
                    exit_default = str(review_sl)
                    exit_default_hint = f"default SL = {review_sl}"
                elif review_outcome == "BE" and review_entry is not None:
                    exit_default = str(review_entry)
                    exit_default_hint = f"default Entry = {review_entry}"

                exit_prompt_label = f"Level Exit / Harga Hit ({exit_default_hint})"
                exit_input = click.prompt(
                    exit_prompt_label,
                    default=exit_default,
                    show_default=bool(exit_default),
                ).strip()
                if exit_input:
                    try:
                        review_exit = float(exit_input)
                    except ValueError:
                        review_exit = None

                notes_input = click.prompt(
                    "Evaluasi / Catatan (misal: reaksi harga di FVG, liquidity sweep, dll.)",
                    default="",
                    show_default=False,
                ).strip()
                if notes_input:
                    review_notes = notes_input

        elif action == "validate":
            validate_setup = True
            click.secho("[Langkah 4/5] Parameter Setup Validasi (SMC v2):", fg="yellow", bold=True)
            while True:
                entry_price = click.prompt("Harga Rencana Entry 1 (Aggressive / Proximal Edge)", type=float)

                e2_input = click.prompt(
                    "Harga Rencana Entry 2 (Moderate / 50% CE/MT, opsional - Enter jika single entry)",
                    default="",
                    show_default=False,
                ).strip()
                if e2_input:
                    try:
                        entry2_price = float(e2_input)
                    except ValueError:
                        entry2_price = None
                else:
                    entry2_price = None

                tp_price = click.prompt("Harga Target TP1 (Nearest DOL)", type=float)

                tp2_input = click.prompt(
                    "Harga Target TP2 (Main DOL, opsional - Enter jika tidak ada)",
                    default="",
                    show_default=False,
                ).strip()
                if tp2_input:
                    try:
                        tp2_price = float(tp2_input)
                    except ValueError:
                        tp2_price = None
                else:
                    tp2_price = None

                sl_input = click.prompt("Harga Stop Loss (opsional, kosongkan jika belum ada)", default="", show_default=False).strip()
                if sl_input:
                    try:
                        sl_price = float(sl_input)
                    except ValueError:
                        sl_price = None
                else:
                    sl_price = None

                # Verifikasi konsistensi hierarki harga secara proaktif
                try:
                    from .trade_plan import build_trade_plan
                    build_trade_plan(
                        entry1=entry_price,
                        entry2=entry2_price,
                        tp1=tp_price,
                        tp2=tp2_price,
                        sl=sl_price,
                    )
                    break
                except SmcPromptError as err:
                    click.echo("")
                    click.secho(f"[!] Parameter setup tidak valid: {err}", fg="bright_red", bold=True)
                    click.secho("    Tips: Pastikan jumlah digit/desimal konsisten dan posisi SL/TP logis terhadap Entry.", fg="yellow")
                    retry = click.confirm(
                        click.style("    Masukkan ulang parameter setup?", fg="bright_white", bold=True),
                        default=True,
                    )
                    if not retry:
                        click.secho("Proses dibatalkan oleh pengguna.", fg="yellow")
                        return
                    click.echo("")

            status_opts = [
                ("unfilled", "Belum Terjemput (Limit order pending di bursa)"),
                ("partial", "Terisi Sebagian (Entry 1 aktif 40%, Entry 2 pending 60%)"),
                ("filled", "Sudah Terjemput (Posisi sedang aktif / running)"),
            ]
            order_status = _prompt_choice("Status Order Anda:", status_opts, default_index=0)

            use_default_val = click.confirm(
                f"Gunakan interval default ({validate_interval}, {validate_candles} candle)?",
                default=True,
            )
            if not use_default_val:
                validate_interval = click.prompt("Interval validasi (e.g. 15m, 5m, 1h)", default=validate_interval)
                validate_candles = click.prompt("Jumlah candle validasi", type=int, default=validate_candles)

        click.echo("")

        # Step 5: Output Preference
        click.secho("[Langkah 5/5] Opsi Tampilan Terminal:", fg="yellow", bold=True)
        print_stdout = click.confirm(
            click.style("Tampilkan seluruh isi teks prompt ke layar terminal (stdout)?", fg="bright_white"),
            default=False,
        )
        click.echo("")

        # Build one-liner CLI command string via pure formatter function
        one_liner = format_cli_command(
            symbol,
            provider=provider,
            input_csv=input_csv,
            twelvedata_key=twelvedata_key,
            oanda_token=oanda_token,
            action=action,
            preset=selected_preset,
            htf_interval=htf_interval,
            mtf_interval=mtf_interval,
            ltf_interval=ltf_interval,
            swing_lookback=swing_lookback,
            candles_only=candles_only,
            review_interval=review_interval,
            review_candles=review_candles,
            review_direction=review_direction,
            review_entry=review_entry,
            review_entry2=review_entry2,
            review_sl=review_sl,
            review_tp=review_tp,
            review_tp2=review_tp2,
            review_outcome=review_outcome,
            review_exit=review_exit,
            review_notes=review_notes,
            validate_setup=validate_setup,
            entry_price=entry_price,
            entry2_price=entry2_price,
            tp_price=tp_price,
            tp2_price=tp2_price,
            sl_price=sl_price,
            order_status=order_status,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
            print_stdout=print_stdout,
        )

        # Summary box
        click.secho("=" * 70, fg="cyan", bold=True)
        click.secho("  RINGKASAN PENGATURAN", fg="bright_white", bold=True)
        click.secho("=" * 70, fg="cyan", bold=True)
        click.echo(f"  * Sumber Data    : {click.style(cfg.provider_label(provider) if provider != 'csv' else 'Offline CSV', fg='bright_yellow', bold=True)}")
        if provider == cfg.PROVIDER_TWELVEDATA:
            key_status = "Tersedia (via .env/environment)" if not twelvedata_key else "Diberikan secara manual"
            click.echo(f"  * Twelve Data Key: {click.style(key_status, fg='cyan')}")
        elif provider == cfg.PROVIDER_OANDA:
            token_status = "Tersedia (via .env/environment)" if not oanda_token else "Diberikan secara manual"
            click.echo(f"  * Token OANDA    : {click.style(token_status, fg='cyan')}")
        click.echo(f"  * Simbol Aset    : {click.style(symbol, fg='bright_green', bold=True)}")
        action_label = "SMC/ICT Prompt Generator" if action == "prompt" else ("Review Pasca-Trade" if action == "review" else "Validasi Setup")
        click.echo(f"  * Mode Tindakan  : {click.style(action_label, fg='bright_white')}")
        if action == "prompt":
            preset_badge = f" [{selected_preset.upper()}]" if selected_preset else " [CUSTOM]"
            click.echo(f"  * Timeframes     : HTF={htf_interval} ({htf_candles}c) | MTF={mtf_interval} ({mtf_candles}c) | LTF={ltf_interval} ({ltf_candles}c){preset_badge}")
        elif action == "review":
            click.echo(f"  * Interval/Count : {review_interval} ({review_candles} candles)")
            if review_direction or review_entry is not None or review_outcome or review_exit is not None:
                parts = []
                if review_direction:
                    parts.append(f"Arah={review_direction.capitalize()}")
                if review_entry is not None:
                    e_str = f"{review_entry}" if review_entry2 is None else f"{review_entry}/{review_entry2}"
                    parts.append(f"Entry={e_str}")
                if review_sl is not None:
                    parts.append(f"SL={review_sl}")
                if review_tp is not None:
                    tp_str = f"{review_tp}" if review_tp2 is None else f"{review_tp}/{review_tp2}"
                    parts.append(f"TP={tp_str}")
                if review_exit is not None:
                    parts.append(f"Exit={review_exit}")
                if review_outcome:
                    parts.append(f"Hasil={review_outcome}")
                click.echo(f"  * Jurnal Trade   : {' | '.join(parts)}")
            if review_notes:
                preview = review_notes if len(review_notes) <= 50 else review_notes[:47] + "..."
                click.echo(f"  * Catatan        : {preview}")
        elif action == "validate":
            e2_desc = f", Entry2={entry2_price}" if entry2_price is not None else ""
            tp2_desc = f", TP2={tp2_price}" if tp2_price is not None else ""
            click.echo(f"  * Setup Params   : Entry1={entry_price}{e2_desc}, TP1={tp_price}{tp2_desc}, SL={sl_price or '-'}, Status={order_status}")
        click.echo(f"  * Output         : File di folder output/ & Otomatis Copy ke Clipboard")
        click.secho("-" * 70, fg="cyan")
        click.echo("  Tips: Perintah one-liner yang ekuivalen:")
        click.secho(f"    {one_liner}", fg="bright_cyan", bold=True)
        click.secho("=" * 70, fg="cyan", bold=True)
        click.echo("")

        execute = click.confirm(
            click.style("Jalankan analisis sekarang?", fg="bright_white", bold=True),
            default=True,
        )
        if not execute:
            click.secho("Proses dibatalkan oleh pengguna.", fg="yellow")
            return

        click.echo("")
        click.secho(f"[*] Menjalankan analisis untuk {symbol} via {cfg.provider_label(provider) if provider != 'csv' else 'Offline CSV'}...", fg="green", bold=True)

        # Import lazily to avoid circular dependency
        from .cli import run

        actual_provider = cfg.PROVIDER_BINANCE if provider == "csv" else provider

        resolved_entry = review_entry if action == "review" else entry_price
        resolved_entry2 = review_entry2 if action == "review" else entry2_price
        resolved_tp = review_tp if action == "review" else tp_price
        resolved_tp2 = review_tp2 if action == "review" else tp2_price
        resolved_sl = review_sl if action == "review" else sl_price
        resolved_direction = review_direction if action == "review" else None

        run(
            symbol,
            preset=selected_preset,
            htf_candles=htf_candles,
            mtf_candles=mtf_candles,
            ltf_candles=ltf_candles,
            htf_interval=htf_interval,
            mtf_interval=mtf_interval,
            ltf_interval=ltf_interval,
            swing_lookback=swing_lookback,
            distance_reference=cfg.DISTANCE_REFERENCE_NEAREST,
            include_atr=True,
            output_dir=cfg.DEFAULT_OUTPUT_DIR,
            print_stdout=print_stdout,
            base_urls=cfg.DEFAULT_BASE_URLS,
            input_csv=input_csv,
            provider=actual_provider,
            twelvedata_key=twelvedata_key,
            oanda_token=oanda_token,
            oanda_account_id=oanda_account_id,
            oanda_env=oanda_env,
            candles_only=candles_only,
            review_interval=review_interval,
            review_candles=review_candles,
            validate_setup=validate_setup,
            entry=resolved_entry,
            tp=resolved_tp,
            sl=resolved_sl,
            direction=resolved_direction,
            outcome=review_outcome,
            notes=review_notes,
            exit_price=review_exit,
            order_status=order_status,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
            entry2=resolved_entry2,
            tp2=resolved_tp2,
        )
        click.secho("\n[+] Selesai! Prompt siap dipaste (Ctrl+V) ke AI kesayangan Anda.", fg="bright_green", bold=True)

    except (click.Abort, KeyboardInterrupt):
        click.echo("")
        click.secho("Operasi dibatalkan (Ctrl+C).", fg="yellow")
        sys.exit(0)
    except SmcPromptError as exc:
        click.echo("")
        click.secho(f"[!] Error: {exc}", fg="bright_red", bold=True)
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # pragma: no cover - defensive
        click.echo("")
        click.secho(f"[!] Kesalahan tak terduga: {exc}", fg="bright_red", bold=True)
        sys.exit(1)
