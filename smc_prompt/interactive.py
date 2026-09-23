"""Terminal Interactive Wizard for smc-prompt-cli.

Provides a guided, step-by-step interactive CLI interface for users
who prefer a conversational prompt instead of remembering one-liner flags.
Includes smart defaults so common configurations (like timeframes) can
be selected with a single ENTER keystroke.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any

import click

from . import config as cfg


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


def run_interactive_wizard() -> None:
    """Run the guided interactive CLI wizard and execute the analysis."""
    _ensure_clean_exit = True
    try:
        _banner()

        # Step 1: Pilih Sumber Data (Data Provider)
        provider_options = [
            (cfg.PROVIDER_BINANCE, "Binance Futures (Crypto USDT-M, tanpa API key)"),
            (cfg.PROVIDER_BITUNIX, "Bitunix Futures (Crypto USDT-M, tanpa API key)"),
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
        review_sl: float | None = None
        review_tp: float | None = None
        review_outcome: str | None = None
        review_notes: str | None = None

        validate_setup = False
        entry_price: float | None = None
        tp_price: float | None = None
        sl_price: float | None = None
        order_status: str | None = None
        validate_interval = cfg.DEFAULT_VALIDATE_INTERVAL
        validate_candles = cfg.DEFAULT_VALIDATE_CANDLES

        if action == "prompt":
            click.secho("[Langkah 4/5] Pengaturan Timeframe & Analisis:", fg="yellow", bold=True)
            click.echo("  Setelan default standar:")
            click.echo(f"    * HTF: {click.style(htf_interval, fg='cyan', bold=True)} ({htf_candles} candle)")
            click.echo(f"    * MTF: {click.style(mtf_interval, fg='cyan', bold=True)} ({mtf_candles} candle)")
            click.echo(f"    * LTF: {click.style(ltf_interval, fg='cyan', bold=True)} ({ltf_candles} candle)")
            click.echo(f"    * Fractal Swing Lookback: {click.style(str(swing_lookback), fg='cyan', bold=True)}")
            use_default_tf = click.confirm(
                click.style("Gunakan setelan default di atas? (Tekan Enter jika Ya)", fg="bright_white"),
                default=True,
            )
            if not use_default_tf:
                htf_interval = click.prompt("HTF interval (e.g. 1d, 4h)", default=htf_interval)
                mtf_interval = click.prompt("MTF interval (e.g. 4h, 2h)", default=mtf_interval)
                ltf_interval = click.prompt("LTF interval (e.g. 1h, 15m)", default=ltf_interval)
                swing_lookback = click.prompt("Swing lookback N (ganjil >= 3)", type=int, default=swing_lookback)

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
                if entry_input:
                    try:
                        review_entry = float(entry_input)
                    except ValueError:
                        review_entry = None

                sl_input = click.prompt(
                    "Stop Loss (SL) (opsional, kosongkan jika belum ada)",
                    default="",
                    show_default=False,
                ).strip()
                if sl_input:
                    try:
                        review_sl = float(sl_input)
                    except ValueError:
                        review_sl = None

                tp_input = click.prompt(
                    "Take Profit (TP) (opsional, kosongkan jika belum ada)",
                    default="",
                    show_default=False,
                ).strip()
                if tp_input:
                    try:
                        review_tp = float(tp_input)
                    except ValueError:
                        review_tp = None

                outcome_opts = [
                    ("Hit TP", "Hit TP (Target Profit Tercapai)"),
                    ("Hit SL", "Hit SL (Stop Loss Kena)"),
                    ("BE", "BE (Break-Even / Impas)"),
                    ("Cut Manual", "Cut Manual (Ditutup Manual Sebelum TP/SL)"),
                ]
                review_outcome = _prompt_choice("Hasil Akhir Trade:", outcome_opts, default_index=0)

                notes_input = click.prompt(
                    "Evaluasi / Catatan (misal: reaksi harga di FVG, liquidity sweep, dll.)",
                    default="",
                    show_default=False,
                ).strip()
                if notes_input:
                    review_notes = notes_input

        elif action == "validate":
            validate_setup = True
            click.secho("[Langkah 4/5] Parameter Setup Validasi:", fg="yellow", bold=True)
            entry_price = click.prompt("Harga Rencana Entry", type=float)
            tp_price = click.prompt("Harga Target TP / DOL", type=float)
            sl_input = click.prompt("Harga Stop Loss (opsional, kosongkan jika belum ada)", default="", show_default=False).strip()
            if sl_input:
                try:
                    sl_price = float(sl_input)
                except ValueError:
                    sl_price = None

            status_opts = [
                ("unfilled", "Belum Terjemput (Limit order pending di bursa)"),
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

        # Build one-liner CLI command string for user's convenience
        cli_parts = [f"smc-prompt {symbol}"]
        if provider != cfg.PROVIDER_BINANCE and provider != "csv":
            cli_parts.append(f"--provider {provider}")
        if provider == "csv":
            cli_parts.append(f"--input-csv {input_csv}")
        if action == "review":
            cli_parts.append("--candles-only")
            if review_interval != cfg.DEFAULT_REVIEW_INTERVAL:
                cli_parts.append(f"--review-interval {review_interval}")
            if review_candles != cfg.DEFAULT_REVIEW_CANDLES:
                cli_parts.append(f"--review-candles {review_candles}")
            if review_direction:
                cli_parts.append(f"--direction {review_direction}")
            if review_entry is not None:
                cli_parts.append(f"--entry {review_entry}")
            if review_sl is not None:
                cli_parts.append(f"--sl {review_sl}")
            if review_tp is not None:
                cli_parts.append(f"--tp {review_tp}")
            if review_outcome:
                cli_parts.append(f'--outcome "{review_outcome}"')
            if review_notes:
                clean_notes = review_notes.replace('"', '\\"')
                cli_parts.append(f'--notes "{clean_notes}"')
        elif action == "validate":
            cli_parts.append("--validate-setup")
            if entry_price is not None:
                cli_parts.append(f"--entry {entry_price}")
            if tp_price is not None:
                cli_parts.append(f"--tp {tp_price}")
            if sl_price is not None:
                cli_parts.append(f"--sl {sl_price}")
            if order_status:
                cli_parts.append(f"--order-status {order_status}")
            if validate_interval != cfg.DEFAULT_VALIDATE_INTERVAL:
                cli_parts.append(f"--validate-interval {validate_interval}")
            if validate_candles != cfg.DEFAULT_VALIDATE_CANDLES:
                cli_parts.append(f"--validate-candles {validate_candles}")
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

        one_liner = " ".join(cli_parts)

        # Summary box
        click.secho("=" * 70, fg="cyan", bold=True)
        click.secho("  RINGKASAN PENGATURAN", fg="bright_white", bold=True)
        click.secho("=" * 70, fg="cyan", bold=True)
        click.echo(f"  * Sumber Data    : {click.style(cfg.provider_label(provider) if provider != 'csv' else 'Offline CSV', fg='bright_yellow', bold=True)}")
        click.echo(f"  * Simbol Aset    : {click.style(symbol, fg='bright_green', bold=True)}")
        action_label = "SMC/ICT Prompt Generator" if action == "prompt" else ("Review Pasca-Trade" if action == "review" else "Validasi Setup")
        click.echo(f"  * Mode Tindakan  : {click.style(action_label, fg='bright_white')}")
        if action == "prompt":
            click.echo(f"  * Timeframes     : HTF={htf_interval} ({htf_candles}c) | MTF={mtf_interval} ({mtf_candles}c) | LTF={ltf_interval} ({ltf_candles}c)")
        elif action == "review":
            click.echo(f"  * Interval/Count : {review_interval} ({review_candles} candles)")
            if review_direction or review_entry is not None or review_outcome:
                parts = []
                if review_direction:
                    parts.append(f"Arah={review_direction.capitalize()}")
                if review_entry is not None:
                    parts.append(f"Entry={review_entry}")
                if review_sl is not None:
                    parts.append(f"SL={review_sl}")
                if review_tp is not None:
                    parts.append(f"TP={review_tp}")
                if review_outcome:
                    parts.append(f"Hasil={review_outcome}")
                click.echo(f"  * Jurnal Trade   : {' | '.join(parts)}")
            if review_notes:
                preview = review_notes if len(review_notes) <= 50 else review_notes[:47] + "..."
                click.echo(f"  * Catatan        : {preview}")
        elif action == "validate":
            click.echo(f"  * Setup Params   : Entry={entry_price}, TP={tp_price}, SL={sl_price or '-'}, Status={order_status}")
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
        resolved_tp = review_tp if action == "review" else tp_price
        resolved_sl = review_sl if action == "review" else sl_price
        resolved_direction = review_direction if action == "review" else None

        run(
            symbol,
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
            order_status=order_status,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
        )
        click.secho("\n[+] Selesai! Prompt siap dipaste (Ctrl+V) ke AI kesayangan Anda.", fg="bright_green", bold=True)

    except (click.Abort, KeyboardInterrupt):
        click.echo("")
        click.secho("Operasi dibatalkan (Ctrl+C).", fg="yellow")
        sys.exit(0)
