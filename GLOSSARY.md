# Glossary

### Render Tail
Jumlah candlestick tertutup (*closed candles*) paling akhir yang dicetak ke dalam tabel prompt CSV (default: 20 HTF, 30 MTF, 40 LTF). Berbeda dari *Fetch Window*, Render Tail bertujuan membatasi konsumsi token LLM tanpa mengorbankan akurasi perhitungan indikator teknis.

### Fetch Window
Jumlah candlestick tertutup yang ditarik dari penyedia data (*data provider*) dan diproses oleh mesin analisis struktur (*Layer-A structure analyzer*) (default: 90 HTF, 186 MTF, 192 LTF). Histori penuh ini diperlukan agar perhitungan ATR(14), sekuens fraktal swing, dan deteksi Fair Value Gap tetap stabil dan tidak terdistorsi.

### Unfilled FVG
Fair Value Gap mekanis (pola 3-candle) yang belum pernah tersentuh atau tertembus penuh oleh rentang harga candle-candle berikutnya dalam deret data tertutup.

### Recently Mitigated FVG
Fair Value Gap yang telah tertembus (*filled*), namun penembusannya terjadi baru saja pada $\le 3$ candle terakhir dari deret data tertutup. Informasi ini dipertahankan sebagai konteks mitigasi likuiditas terkini bagi model AI.

### Consequent Encroachment (CE)
Titik tengah (level 50%) dari rentang suatu Fair Value Gap (`ce = (lower + upper) / 2`). Berfungsi sebagai level keseimbangan internal inefisiensi harga dan acuan penempatan *Moderate Entry* (`Entry 2`).
_Avoid_: FVG Midpoint, Gap Halfway, 50% Gap.

### Compact Timestamp
Format penulisan waktu ringkas tanpa prefiks tahun yang ditentukan berdasarkan durasi interval: `MM-DD` untuk timeframe harian ke atas ($\ge 1\text{d}$) dan `MM-DD HH:MM` untuk seluruh timeframe intraday ($< 1\text{d}$, termasuk `4h`, `1h`, `15m`). Tahun referensi utama tetap tercatat di header dokumen (`Waktu generate (UTC)`).

### Dealing Range
Rentang kerja harga aktif pada suatu timeframe yang dibentuk secara mekanis oleh pasangan *Recent Swing Low* dan *Recent Swing High* terakhir yang terdeteksi fraktal.
_Avoid_: Trading Range, Price Channel, Consolidation Box.

### Equilibrium (EQ)
Titik tengah (level 50%) dari suatu *Dealing Range* (`eq = (recent_low + recent_high) / 2`) yang membagi rentang harga menjadi zona *Premium* (di atas EQ, ideal untuk mencari peluang *Short*) dan zona *Discount* (di bawah EQ, ideal untuk mencari peluang *Long*).
_Avoid_: Midpoint, Fair Price, Halfway Level.

### Inducement (IDM)
Likuiditas internal (*Internal Range Liquidity*) berupa klaster Stop Loss ritel terdekat atau level swing minor yang menjadi target sapuan manipulatif (*sweep zone*). Berfungsi sebagai syarat pemicu wajib sebelum konfirmasi *entry* pada skema *Sweep / Reversal* (saat struktur LTF belum searah), namun bersifat opsional untuk dilompati pada skema *Trend Continuation* ketika struktur MTF dan LTF sudah selaras dengan bias HTF dan memiliki POI dekat yang belum termitigasi.
_Avoid_: Entry Pool, Minor Support/Resistance, Trap Level.

### Draw on Liquidity (DOL)
Likuiditas eksternal (*External Range Liquidity*) atau inefisiensi harga di sisi berlawanan—mencakup klaster Stop Loss ritel mayor, *Equal Highs/Lows*, batas *Dealing Range*, atau *Unfilled FVG*—yang menjadi magnet tujuan akhir harga sekaligus target *Take Profit* (TP1/TP2).
_Avoid_: Final Target, Resistance/Support Target, Exit Zone.

### Market Structure Shift (MSS)
Perubahan arah struktur pasar awal (*Change of Character / CHoCH*) yang terkonfirmasi ketika candlestick berhasil *close* melampaui level swing berlawanan terakhir setelah terjadinya *liquidity sweep* pada zona *Inducement*.
_Avoid_: Trend Reversal, Early BOS.

### Killzone
Jendela waktu sesi perdagangan institusional berbasis UTC tempat manipulasi likuiditas (*judas swing*) dan ekspansi harga paling sering terjadi, dipetakan secara deterministik menjadi: *Asia Session* (`00:00–06:59 UTC`), *London Open Killzone* (`07:00–09:59 UTC`), *London Session / Pre-NY* (`10:00–11:59 UTC`), *New York Open Killzone* (`12:00–14:59 UTC`), *London Close / NY Afternoon* (`15:00–19:59 UTC`), dan *Late NY / Off-Session* (`20:00–23:59 UTC`).
_Avoid_: Trading Hours, Market Open, Timezone Window.

### SMT Divergence
Ketidakselarasan struktur swing (*Smart Money Technique Divergence*) antara aset yang dianalisis (seperti altcoin) dan aset acuan berkorelasi tinggi (`BTCUSDT`) pada titik waktu yang setara—misalnya ketika satu aset mencetak *Higher High* sementara aset acuan gagal menembus *Swing High* sebelumnya—yang mengindikasikan kejenuhan tren atau sapuan likuiditas.
_Avoid_: Indicator Divergence, Correlation Lag.

### Order Block (OB)
Candlestick berbasis zona penumpukan likuiditas institusional (*base candle*) sebelum terjadinya *rally* atau *drop* eksplosif (*displacement*). Ditetapkan secara deterministik hanya jika diikuti oleh terbentuknya Fair Value Gap (FVG-Linked OB) dengan ukuran gap $\ge 0.1 \times \text{ATR}$.

### Mean Threshold (MT)
Titik tengah (level 50%) dari rentang suatu Order Block (`mt = (low + high) / 2`). Berfungsi sebagai batas reaksi kritis di mana penetrasi harga melebihi MT mengindikasikan melemahnya kekuatan penolakan zona OB.

### Proximal Edge
Batas terluar dari suatu zona Point of Interest (FVG atau Order Block) yang paling dekat dengan harga pasar saat ini—yaitu batas atas (`upper`) pada zona *Bullish* dan batas bawah (`lower`) pada zona *Bearish*—yang menjadi titik acuan dasar bagi penempatan *Aggressive Entry* (`Entry 1`).
_Avoid_: Outer Boundary, Near Edge, Upper/Lower Lip.

### Front-Run Buffer
Bantalan harga berbasis volatilitas (`0.10 × ATR(14) LTF`) yang ditambahkan di depan *Proximal Edge* suatu zona POI (ditambahkan di atas `upper` untuk *Long*, dikurangkan di bawah `lower` untuk *Short*) guna mengantisipasi pembalikan harga prematur (*front-run*) sebelum menyentuh batas mekanis zona.
_Avoid_: Spread Padding, Entry Offset, Fudge Factor.

### Blended Entry
Harga masuk rata-rata tertimbang (`(0.4 × Entry 1) + (0.6 × Entry 2)`) dari skema dua tingkat harga (*Aggressive* dan *Moderate*) yang digunakan sebagai titik acuan tunggal dalam perhitungan jarak risiko (`1R`) dan kelayakan *Risk-to-Reward Ratio* (RRR) menuju target *Draw on Liquidity* (TP1/TP2).
_Avoid_: Average Fill, Split Midpoint, Composite Entry.

### Wick Sweep
Penolakan manipulatif 1-candle (*1-Candle Rejection*) di mana *wick* candlestick sempat menembus level Swing High atau Swing Low terdahulu, tetapi *body close* kembali ke dalam rentang level tersebut.

### Reclaim Sweep
Manipulasi likuiditas 2-candle (*2-Candle Stop Hunt*) di mana candlestick pertama sempat *close* tipis di luar level swing, namun candlestick berikutnya langsung *close* kembali ke dalam rentang level swing.

### Break of Structure (BOS)
Penembusan struktural riil (*Structural Break*) di mana harga menembus level swing dan berhasil bertahan dengan *close* di luar level tersebut selama minimal 2 candlestick berturut-turut ($\ge 2$ consecutive closes). Membedakan kelanjutan tren dari manipulasi likuiditas (*sweep*).

### Crypto Failover Chain
Mekanisme pengalihan otomatis berurutan antar-exchange kripto publik (`binance` ➔ `bitunix` ➔ `mexc` ➔ `gate`) saat terjadi kegagalan koneksi jaringan (`NetworkError`), guna menjamin kontinuitas penarikan data pasar tanpa interupsi manual.
_Avoid_: Provider Fallback, Exchange Redundancy, Network Retry.

### Strict Provider Mode
Mode eksekusi di mana pengalihan penyedia data otomatis dinonaktifkan (`--no-fallback`), memaksa perintah gagal seketika jika exchange yang diminta secara eksplisit tidak dapat dihubungi.
_Avoid_: Hard Mode, Fail-Fast Flag, No-Failover.

### Partial Fill
Kondisi eksekusi perdagangan di mana hanya sebagian alokasi order yang terisi di bursa—khususnya pada skema *Dual-Entry* di mana *Entry 1 (Aggressive / 40% size)* telah terisi dan aktif berjalan, sementara *Entry 2 (Moderate / 60% size)* masih berstatus *pending limit order*.

### Maximum Favorable Excursion (MFE)
Puncak keuntungan harga menguntungkan terbesar (*peak unrealized profit*) yang sempat dicapai oleh pergerakan pasar setelah titik masuk perdagangan terpicu, dinyatakan dalam nominal poin harga dan kelipatan risiko terencana ($+X.XX\text{R}$).

### Maximum Adverse Excursion (MAE)
Penetrasi kerugian berlawanan terdalam (*peak unrealized drawdown*) yang sempat ditahan oleh posisi perdagangan sebelum berbalik arah atau menyentuh target/stop loss, dinyatakan dalam nominal poin harga dan kelipatan risiko terencana ($-X.XX\text{R}$).

### First-Touch Sequence
Kronologi urutan pertama kali harga menyentuh level-level kritis perdagangan (*entry*, *Stop Loss*, *TP1*, *TP2*) yang dievaluasi secara sekuensial candle-demi-candle guna membuktikan apakah target tercapai sebelum stop loss tersentuh tanpa bias observasi visual.

### Trading Style Preset
Konfigurasi terstandardisasi untuk analisis multi-timeframe 3-Tier (HTF, MTF, LTF) yang diselaraskan dengan ritme eksekusi dan durasi holding trading, mencakup preset `intraday` (default: 1d/1h/15m), `sniper` (4h/1h/5m), `balanced` (4h/1h/15m), `swing` (1d/4h/1h), dan `scalp` (1h/15m/5m).
_Avoid_: Timeframe Profile, Strategy Mode, Chart Template.

