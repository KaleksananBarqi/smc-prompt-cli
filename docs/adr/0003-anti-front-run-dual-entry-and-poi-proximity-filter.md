# Anti-Front-Run Dual-Entry Schema, Consequent Encroachment, and ATR-Based POI Proximity Filtering

Mengatasi tingginya tingkat *front-run* (~75%) pada rencana perdagangan yang dihasilkan LLM dengan merombak skema eksekusi dan perhitungan RRR pada template prompt menjadi dua tingkat harga (*Entry 1 — Aggressive* dan *Entry 2 — Moderate* dengan acuan *Blended Entry*), mengekspos nilai tengah 50% Fair Value Gap (*Consequent Encroachment* / `ce`) secara mekanis di Layer A, serta menerapkan filter jarak berbasis ATR (`<= 5.0 × ATR`) khusus pada tabel POI (FVG dan Order Block) di timeframe MTF dan LTF.

## Status
Accepted

## Konteks
Evaluasi empiris terhadap output analisis SMC menunjukkan bahwa meskipun akurasi arah bias mencapai ~85%, sekitar 75% *limit order* gagal terjemput (*front-runned*). Akar masalahnya berasal dari interaksi empat faktor di dalam prompt dan payload data:
1. **Tekanan Matematis RRR TP1 ≥ 1:2:** Aturan di Bagian 5 dan 6 menghukum skor konfluensi jika `RRR TP1 < 1:2`. Karena level SL dan TP1 terkunci pada struktur swing/likuiditas, LLM selalu memaksakan titik Entry masuk sedalam mungkin mendekati SL demi mencapai angka `2.0R` di TP1.
2. **Kewajiban Mutlak Sapuan IDM & Lag Klasifikasi LTF:** Bagian 4 mewajibkan *liquidity sweep* pada *Inducement* (IDM) untuk seluruh kondisi pasar. Saat tren kuat (*continuation*), harga kerap langsung memantul dari FVG/OB terdekat tanpa menyapu swing ritel. Selain itu, klasifikasi struktur mekanis LTF (`LTF_STRUCTURE_CLASS`) menggunakan fractal 5-bar yang tertinggal minimal 2 candle tertutup dan jatuh ke `Ranging/Mixed` saat terjadi `pending_break`, sehingga LLM ragu mengambil skenario kelanjutan tren.
3. **Absennya Titik Tengah FVG (`ce`) & Dominasi `mt` (50% OB):** Tabel Order Block menampilkan kolom `mt` (50%), sedangkan FVG hanya menampilkan `lower` dan `upper`. Hal ini mendorong LLM mengabaikan *Proximal Edge* (batas terluar) POI.
4. **Polusi FVG/OB Usang pada LTF:** Tabel FVG dan OB merender hingga `N` zona terbaru yang belum termitigasi tanpa batas jarak. Pada pergerakan impulsif, FVG LTF yang tertinggal sangat jauh (misal `> 15 × ATR LTF` dari harga saat ini) tetap dicetak dan mengalihkan perhatian LLM ke zona ekstrem bawah/atas.
5. **Kompatibilitas Sistem Hilir (`Trawangan`):** Mesin *Deterministic Trading Plan Ingestion* (`planParserService.ts`) di aplikasi charting hilir mengekstrak harga masuk dari baris tabel Markdown yang memiliki kunci persis `Entry`. Mengubah nama baris tanpa mempertahankan kunci `Entry` akan memutus integrasi *Smart Canvas Paste*.

## Keputusan
1. **Dua Mode Eksekusi di Bagian 4 (Continuation vs Sweep/Reversal):**
   - **Mode Continuation (Tren Kuat):** Aktif apabila MTF searah dengan bias HTF, struktur LTF sudah searah **atau** menunjukkan `pending_break`/`broken` (BOS/MSS) searah bias, serta terdapat *Unfilled FVG* / *Unmitigated OB* LTF dekat harga saat ini (`<= 1.5 × ATR LTF`). Eksekusi tidak wajib menunggu sapuan IDM dalam.
   - **Mode Sweep / Reversal (Ranging/Counter):** Aktif apabila struktur LTF belum searah; wajib menunggu `wick_sweep` / `reclaim_sweep` pada zona IDM diikuti konfirmasi MSS LTF.
2. **Skema Dual-Level Entry + Blended Entry yang Kompatibel dengan Parser Hilir (Bagian 5):**
   - Menyediakan tiga baris terstruktur pada Tabel Trading Plan dengan 1 Stop Loss struktural yang sama:
     - `Entry 1 (Aggressive)`: *Proximal Edge* POI LTF terdekat ditambah *Front-Run Buffer* `0.10 × ATR(14) LTF` (`upper + 0.10×ATR` untuk Long, `lower - 0.10×ATR` untuk Short), dengan *guard* tidak boleh melampaui `Current price` untuk *pending limit order* (40% size atau opsi mandiri agresif).
     - `Entry 2 (Moderate)`: Level *Consequent Encroachment* (`ce` 50% FVG), *Mean Threshold* (`mt` 50% OB), atau zona pasca-sapuan IDM (60% size atau opsi mandiri moderat).
     - `Entry`: Harga rata-rata tertimbang *Blended Entry* (`(0.4 × Entry 1) + (0.6 × Entry 2)`) sebagai acuan kalkulasi `1R` dan `RRR`, sekaligus menjaga kompatibilitas 100% dengan ekstraktor `cleanKey === 'entry'` pada `planParserService.ts`.
3. **Relaksasi Aturan RRR (Bagian 5 & 6):**
   - Syarat skor konfluensi `(+1) RRR >= 1:2` digeser perhitungannya menggunakan jarak **Blended Entry ke TP2 (Main DOL)**.
   - Syarat kelayakan minimum untuk **TP1 (Nearest DOL)** dilonggarkan menjadi `>= 1:1.2` (dari `Entry 1`) atau `>= 1:1.5` (dari `Blended Entry`).
4. **Kolom *Consequent Encroachment* (`ce`) pada FVG (Layer A):**
   - Menambahkan properti mekanis `ce = (lower + upper) / 2` pada model `FVG` dan merendernya ke dalam tabel FVG di seluruh timeframe dengan format `stamp,tipe,lower,upper,ce,status`.
5. **Filter Kedekatan POI Berbasis ATR Khusus MTF & LTF (`max_poi_atr_mult = 5.0`):**
   - Pada MTF dan LTF, tabel FVG dan OB hanya merender zona aktif yang jarak terdekatnya ke `current_price` bernilai `<= 5.0 × ATR(14)` timeframe terkait (merender `NONE` bila kosong).
   - Tabel **HTF tidak dikenakan filter jarak ATR** agar POI di batas *Dealing Range* makro dan target *Draw on Liquidity* (DOL) eksternal tetap terlihat utuh oleh LLM.

## Konsekuensi
- LLM berhenti memaksakan harga limit order terlalu dalam demi mengejar 2R pada TP1, sambil tetap memberikan opsi tingkat harga agresif dan moderat yang terukur bagi trader.
- Tabel FVG dan OB pada MTF/LTF bebas dari polusi zona usang yang tertinggal jauh dari harga berjalan, sementara peta struktur makro HTF tetap lengkap.
- Kontrak pengujian *golden render* dan unit test terkait format tabel FVG/prompt perlu diperbarui mengikuti penambahan kolom `ce` dan instruksi Bagian 4–6.
