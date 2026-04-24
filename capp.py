import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, datetime

st.set_page_config(page_title="Ev Bütçesi ve Borç Takibi", layout="wide")

# ─────────────────────────────────────────────────────────────────────────────
# SABİTLER
# ─────────────────────────────────────────────────────────────────────────────
AY_MAP = {
    "ocak":1,"şubat":2,"mart":3,"nisan":4,
    "mayıs":5,"haziran":6,"temmuz":7,"ağustos":8,
    "eylül":9,"ekim":10,"kasım":11,"aralık":12,
}
NO_AY = {v: k for k, v in AY_MAP.items()}

# VB1 için sabit minimum ödeme
VB1_SABIT_MIN = 20_000.0
# Kredi kartı minimum ödeme oranı
KK_MIN_ORAN = 0.40

def para_fmt(x):
    return f"{x:,.0f} ₺".replace(",", ".")

def temizle_sayi(seri):
    return (
        seri.astype(str)
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .pipe(pd.to_numeric, errors="coerce")
        .fillna(0)
    )

def gun_no(tarih_str):
    """'02.03.2026' → gün numarası (int)"""
    try:
        return int(str(tarih_str).strip().split(".")[0])
    except:
        return 99

# ─────────────────────────────────────────────────────────────────────────────
# VERİ YÜKLEME
# NOT: gelir.csv → gider tabloları, gider.csv → gelir tablosu (dosyalar ters)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data
def load_all():
    # ── GİDER VERİSİ (gider.csv) ─────────────────────────────────────────
    graw = pd.read_csv("gider.csv", sep=";", header=None, engine="python", on_bad_lines="skip")

    def temizle(df, cols):
        for c in cols:
            df[c] = temizle_sayi(df[c])
        return df

    # İhtiyaç kredisi (satır 2-15, index 2:16)
    ihtiyac_cols = ["sıra","tarih_sıra","banka","aylık_borç","ödeme_günü",
                    "taksit_sayısı","toplam_borç","şuank_borç","faiz","fark","bitiş_tarihi"]
    df_ihtiyac = graw.iloc[2:16].copy().iloc[:, :len(ihtiyac_cols)]
    df_ihtiyac.columns = ihtiyac_cols
    df_ihtiyac = df_ihtiyac[df_ihtiyac["sıra"].notna() & (df_ihtiyac["sıra"].astype(str).str.strip() != "")]
    df_ihtiyac = temizle(df_ihtiyac, ["aylık_borç","şuank_borç","toplam_borç"])
    df_ihtiyac["gun"] = df_ihtiyac["ödeme_günü"].apply(gun_no)

    # KHM (satır 19-26)
    df_khm = graw.iloc[19:27].copy().iloc[:, :4]
    df_khm.columns = ["banka","toplam_borç","kalan_limit","aylık_borç"]
    df_khm = df_khm[df_khm["banka"].notna() & (df_khm["banka"].astype(str).str.strip() != "")]
    df_khm = temizle(df_khm, ["aylık_borç","toplam_borç"])
    # KHM'de ödeme günü yok → 1. gün varsayımı
    df_khm["gun"] = 1

    # Kredi kartı ham (satır 31-37)
    df_kk_raw = graw.iloc[31:38].copy().iloc[:, :6]
    df_kk_raw.columns = ["banka","toplam_borç","kalan_limit","aylık_borç","hesap_kesim","ödeme_günü"]
    df_kk_raw = df_kk_raw[df_kk_raw["banka"].notna() & (df_kk_raw["banka"].astype(str).str.strip() != "")]
    df_kk_raw = temizle(df_kk_raw, ["toplam_borç","kalan_limit","aylık_borç"])
    df_kk_raw["gun"] = df_kk_raw["ödeme_günü"].apply(gun_no)

    # Münferit (satır 41-48)
    df_mun = graw.iloc[41:49].copy().iloc[:, :2]
    df_mun.columns = ["ay","aylık_borç"]
    df_mun = df_mun[df_mun["ay"].notna() & (df_mun["ay"].astype(str).str.strip() != "")]
    df_mun["aylık_borç"] = temizle_sayi(df_mun["aylık_borç"])
    df_mun["ay"] = df_mun["ay"].str.lower().str.strip()
    munferit_dict = dict(zip(df_mun["ay"], df_mun["aylık_borç"]))

    # ── GELİR VERİSİ (gelir.csv) ─────────────────────────────────────────
    araw = pd.read_csv("gelir.csv", sep=";", header=None, engine="python", on_bad_lines="skip")
    df_gelir = araw.iloc[2:].copy().iloc[:, [0, 7]]
    df_gelir.columns = ["ay","toplam_gelir"]
    df_gelir["ay"] = df_gelir["ay"].astype(str).str.lower().str.strip()
    df_gelir = df_gelir[df_gelir["ay"].isin(AY_MAP.keys())]
    df_gelir["toplam_gelir"] = temizle_sayi(df_gelir["toplam_gelir"])

    return df_ihtiyac, df_khm, df_kk_raw, munferit_dict, df_gelir


# ─────────────────────────────────────────────────────────────────────────────
# KREDİ KARTI PROJEKSIYON (aylık dinamik hesap)
# ─────────────────────────────────────────────────────────────────────────────

def kk_projeksiyon(df_kk_raw, ay_listesi):
    """
    Her ay için kredi kartı durumunu hesaplar.
    Kural: kalan_borç(n+1) = kalan_borç(n) - min_odeme(n)
           min_odeme(n+1)  = kalan_borç(n+1) * %40
    VB1 istisnası: min_odeme sabit 20.000
    """
    rows = []
    # Başlangıç durumu
    state = {}
    for _, r in df_kk_raw.iterrows():
        banka = str(r["banka"]).strip().lower()
        state[banka] = {
            "banka_label": str(r["banka"]).strip(),
            "kalan_borc":  float(r["toplam_borç"]),
            "gun":         int(r["gun"]),
            "ödeme_günü":  str(r["ödeme_günü"]),
        }

    for ay in ay_listesi:
        ay_rows = {}
        for banka, s in state.items():
            kb = max(s["kalan_borc"], 0)
            if "vb 1" in banka or "vb1" in banka:
                min_od = VB1_SABIT_MIN if kb >= VB1_SABIT_MIN else kb
            else:
                min_od = round(kb * KK_MIN_ORAN)
            ay_rows[banka] = {
                "banka":       s["banka_label"],
                "kalan_borc":  kb,
                "min_odeme":   min_od,
                "gun":         s["gun"],
                "ödeme_günü":  s["ödeme_günü"],
            }
        rows.append({"ay": ay, "kartlar": ay_rows})

        # Sonraki ay için güncelle
        for banka in state:
            kb    = max(state[banka]["kalan_borc"], 0)
            if "vb 1" in banka or "vb1" in banka:
                min_od = VB1_SABIT_MIN if kb >= VB1_SABIT_MIN else kb
            else:
                min_od = round(kb * KK_MIN_ORAN)
            state[banka]["kalan_borc"] = max(kb - min_od, 0)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# ÖDEME TAKVİMİ (ay içi günlük sıralama)
# ─────────────────────────────────────────────────────────────────────────────

def odeme_takvimi(ay, df_ihtiyac, df_khm, kk_ay_dict, munferit):
    """Verilen ay için tüm ödemeleri günlük sıralı liste döndürür."""
    kayitlar = []

    # İhtiyaç kredileri
    for _, r in df_ihtiyac.iterrows():
        kayitlar.append({
            "Gun": int(r["gun"]),
            "Tür": "İhtiyaç Kredisi",
            "Açıklama": str(r["banka"]),
            "Tutar": float(r["aylık_borç"]),
            "Ödeme Tarihi": str(r["ödeme_günü"]),
        })

    # KHM
    for _, r in df_khm.iterrows():
        kayitlar.append({
            "Gun": 1,
            "Tür": "KHM",
            "Açıklama": str(r["banka"]),
            "Tutar": float(r["aylık_borç"]),
            "Ödeme Tarihi": "01.??.????",
        })

    # Kredi kartları
    for banka, d in kk_ay_dict.items():
        kayitlar.append({
            "Gun": d["gun"],
            "Tür": "Kredi Kartı",
            "Açıklama": d["banka"],
            "Tutar": d["min_odeme"],
            "Ödeme Tarihi": d["ödeme_günü"],
        })

    # Münferit (15. gün)
    if munferit > 0:
        kayitlar.append({
            "Gun": 15,
            "Tür": "Münferit",
            "Açıklama": "Münferit Ödeme",
            "Tutar": munferit,
            "Ödeme Tarihi": f"15.{AY_MAP.get(ay,0):02d}.????",
        })

    df = pd.DataFrame(kayitlar).sort_values("Gun").reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ANA UYGULAMA
# ─────────────────────────────────────────────────────────────────────────────

try:
    df_ihtiyac, df_khm, df_kk_raw, munferit_dict, df_gelir = load_all()

    bugun       = date.today()
    tarih_str   = bugun.strftime("%d.%m.%Y")
    bugun_ay_no = bugun.month
    if bugun_ay_no == 4:
        bugun_ay_no = 5          # Nisan → Mayıs göster
    bugun_ay    = NO_AY.get(bugun_ay_no, "mayıs")

    # Gelir.csv'deki aylar sırası
    gelir_aylar = df_gelir["ay"].tolist()

    # KK projeksiyonu tüm aylar için
    kk_proj = kk_projeksiyon(df_kk_raw, gelir_aylar)
    kk_proj_dict = {p["ay"]: p["kartlar"] for p in kk_proj}

    # Bugünün ayı için değerler
    ay_gelir_row = df_gelir[df_gelir["ay"] == bugun_ay]
    ay_geliri    = float(ay_gelir_row["toplam_gelir"].values[0]) if not ay_gelir_row.empty else 0

    ihtiyac_taksit = df_ihtiyac["aylık_borç"].sum()
    ihtiyac_borc   = df_ihtiyac["şuank_borç"].sum()
    khm_taksit     = df_khm["aylık_borç"].sum()
    khm_borc       = df_khm["toplam_borç"].sum()

    kk_bugun       = kk_proj_dict.get(bugun_ay, {})
    kk_taksit      = sum(d["min_odeme"] for d in kk_bugun.values())
    kk_borc        = sum(d["kalan_borc"] for d in kk_bugun.values())
    toplam_borc    = ihtiyac_borc + khm_borc + kk_borc

    munferit_bugun = munferit_dict.get(bugun_ay, 0)
    genel_taksit   = ihtiyac_taksit + khm_taksit + kk_taksit + munferit_bugun
    net_kalan      = ay_geliri - genel_taksit

    # ── ÖDEME GÜNÜ POPUP ─────────────────────────────────────────────────
    # Bugün herhangi bir ödeme günü mü?
    bugun_gun = bugun.day
    odeme_bugun = []

    for _, r in df_ihtiyac.iterrows():
        if int(r["gun"]) == bugun_gun:
            odeme_bugun.append(f"🏦 İhtiyaç Kredisi – {r['banka']} ({para_fmt(r['aylık_borç'])})")

    for _, r in df_khm.iterrows():
        if bugun_gun == 1:
            odeme_bugun.append(f"📋 KHM – {r['banka']} ({para_fmt(r['aylık_borç'])})")

    for banka, d in kk_bugun.items():
        if d["gun"] == bugun_gun:
            odeme_bugun.append(f"💳 Kredi Kartı – {d['banka']} ({para_fmt(d['min_odeme'])})")

    if bugun_gun == 15 and munferit_bugun > 0:
        odeme_bugun.append(f"🟣 Münferit Ödeme ({para_fmt(munferit_bugun)})")

    if odeme_bugun:
        odeme_html = "".join(f"<div style='margin:6px 0;font-size:1rem;'>• {x}</div>" for x in odeme_bugun)
        st.markdown(
            f"""
            <div style='
                background: linear-gradient(135deg,#1a1a2e,#16213e);
                border: 2px solid #f72585;
                border-radius: 14px;
                padding: 20px 28px;
                margin-bottom: 20px;
                box-shadow: 0 0 24px rgba(247,37,133,0.4);
            '>
            <div style='font-size:1.6rem;font-weight:700;color:#f72585;margin-bottom:10px;'>
                🌧️ Ödeme Günü Geldi!
            </div>
            <div style='font-size:1.05rem;color:#ccc;margin-bottom:12px;'>
                Bugün <b style='color:white'>{tarih_str}</b> tarihli ödemelerin var. Ödemeni yaptın mı?
            </div>
            {odeme_html}
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Yağmur animasyonu (CSS)
        st.markdown("""
        <style>
        @keyframes fall { 0%{transform:translateY(-20px);opacity:0} 10%{opacity:1} 90%{opacity:1} 100%{transform:translateY(100vh);opacity:0} }
        .rain-wrap { position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:9999;overflow:hidden; }
        .drop { position:absolute;top:0;width:2px;border-radius:2px;background:linear-gradient(transparent,#58c4f5);animation:fall linear infinite; }
        </style>
        <div class='rain-wrap'>
        """ + "".join(
            f"<div class='drop' style='left:{i*3.5}%;height:{12+i%8}px;animation-duration:{0.7+(i%5)*0.15}s;animation-delay:{(i%10)*0.08}s;opacity:0.6'></div>"
            for i in range(28)
        ) + "</div>", unsafe_allow_html=True)

    # ── BAŞLIK ───────────────────────────────────────────────────────────
    bas_sol, bas_sag = st.columns([3, 1])
    with bas_sol:
        st.title("🏠 Ev Bütçesi ve Borç Takibi")
    with bas_sag:
        st.markdown(
            f"<div style='text-align:right;padding-top:22px;font-size:1.15rem;color:#aaa;'>📅 {tarih_str}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── BORÇ DURUMU ───────────────────────────────────────────────────────
    st.markdown("#### 💳 Güncel Borç Durumu")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🏦 İhtiyaç Kredisi Borcu", para_fmt(ihtiyac_borc))
    c2.metric("📋 KHM Borcu",             para_fmt(khm_borc))
    c3.metric("💳 Kredi Kartı Borcu",      para_fmt(kk_borc))
    c4.metric("📉 Toplam Borç",            para_fmt(toplam_borc))

    st.markdown("---")

    # ── AYLIK NAKİT AKIŞI ────────────────────────────────────────────────
    st.markdown(f"#### 📊 {bugun_ay.capitalize()} Ayı Nakit Akışı")
    d1, d2, d3, d4, d5, d6 = st.columns(6)
    d1.metric("🔴 İhtiyaç Taksit", para_fmt(ihtiyac_taksit))
    d2.metric("🟠 KHM Taksit",     para_fmt(khm_taksit))
    d3.metric("🟡 KK Min. Ödeme",  para_fmt(kk_taksit))
    d4.metric("🟣 Münferit",        para_fmt(munferit_bugun))
    d5.metric("💵 Toplam Gelir",    para_fmt(ay_geliri))
    d6.metric(
        "✅ Net Kalan" if net_kalan >= 0 else "⚠️ Net Kalan",
        para_fmt(net_kalan),
    )

    st.markdown("---")

    # ── SEKMELER ─────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📋 İhtiyaç Kredileri", "🏦 KHM Kredileri",
        "💳 Kredi Kartları", "📅 Ödeme Takvimi", "📊 Genel Analiz"
    ])

    # ── TAB1: İHTİYAÇ KREDİLERİ ─────────────────────────────────────────
    with tab1:
        st.subheader("İhtiyaç Kredisi Detayları")
        show = df_ihtiyac[["banka","aylık_borç","şuank_borç","taksit_sayısı","faiz","bitiş_tarihi"]].copy()
        show.columns = ["Banka","Aylık Taksit (₺)","Kalan Borç (₺)","Kalan Taksit","Faiz (%)","Bitiş Tarihi"]
        st.dataframe(show, use_container_width=True, hide_index=True)
        fig1 = px.bar(
            df_ihtiyac.sort_values("aylık_borç", ascending=True),
            x="aylık_borç", y="banka", orientation="h",
            title="Bankaya Göre Aylık Taksit",
            labels={"aylık_borç":"Aylık Taksit (₺)","banka":"Banka"},
            color="aylık_borç", color_continuous_scale="Blues",
        )
        st.plotly_chart(fig1, use_container_width=True)

    # ── TAB2: KHM ───────────────────────────────────────────────────────
    with tab2:
        st.subheader("KHM (Kısa Vadeli) Kredi Detayları")
        show_khm = df_khm[["banka","aylık_borç","toplam_borç","kalan_limit"]].copy()
        show_khm.columns = ["Banka","Aylık Ödeme (₺)","Toplam Borç (₺)","Kalan Limit (₺)"]
        st.dataframe(show_khm, use_container_width=True, hide_index=True)

    # ── TAB3: KREDİ KARTLARI ─────────────────────────────────────────────
    with tab3:
        st.subheader("Kredi Kartı – Aylık Projeksiyon")
        st.caption("Min. ödeme = kalan borcun %40'ı | VB1 sabit 20.000 ₺ | Her ay önceki ayın min. ödemesi düşülür")

        ay_sec_kk = st.selectbox(
            "Ay seçin",
            options=gelir_aylar,
            format_func=lambda x: x.capitalize(),
            key="kk_ay"
        )
        kk_sec = kk_proj_dict.get(ay_sec_kk, {})

        kk_rows = []
        for banka, d in kk_sec.items():
            kk_rows.append({
                "Banka":          d["banka"],
                "Kalan Borç (₺)": para_fmt(d["kalan_borc"]),
                "Min. Ödeme (₺)": para_fmt(d["min_odeme"]),
                "Ödeme Günü":     d["ödeme_günü"],
            })
        df_kk_show = pd.DataFrame(kk_rows)
        st.dataframe(df_kk_show, use_container_width=True, hide_index=True)

        # Pasta: seçili ayda KK borç dağılımı
        pie_data = pd.DataFrame([
            {"Banka": d["banka"], "Kalan Borç": d["kalan_borc"]}
            for d in kk_sec.values() if d["kalan_borc"] > 0
        ])
        if not pie_data.empty:
            fig_pie_kk = px.pie(pie_data, names="Banka", values="Kalan Borç",
                                title=f"{ay_sec_kk.capitalize()} – KK Borç Dağılımı", hole=0.4)
            st.plotly_chart(fig_pie_kk, use_container_width=True)

        # Aylık KK min. ödeme trendi
        st.markdown("#### KK Min. Ödeme Trendi (Tüm Aylar)")
        trend_rows = []
        for p in kk_proj:
            toplam_min = sum(d["min_odeme"] for d in p["kartlar"].values())
            toplam_kb  = sum(d["kalan_borc"] for d in p["kartlar"].values())
            trend_rows.append({"Ay": p["ay"].capitalize(), "Min. Ödeme": toplam_min, "Kalan Borç": toplam_kb})
        df_trend = pd.DataFrame(trend_rows)

        fig_trend = go.Figure()
        fig_trend.add_trace(go.Bar(name="Min. Ödeme", x=df_trend["Ay"], y=df_trend["Min. Ödeme"],
                                   marker_color="#EF553B",
                                   text=df_trend["Min. Ödeme"].apply(lambda v: f"{v/1000:.0f}K"),
                                   textposition="outside"))
        fig_trend.add_trace(go.Scatter(name="Kalan Borç", x=df_trend["Ay"], y=df_trend["Kalan Borç"],
                                       mode="lines+markers", yaxis="y2",
                                       line=dict(color="#636EFA", width=2),
                                       marker=dict(size=7)))
        fig_trend.update_layout(
            yaxis=dict(title="Min. Ödeme (₺)"),
            yaxis2=dict(title="Kalan Borç (₺)", overlaying="y", side="right"),
            legend=dict(orientation="h", y=1.1),
            height=380,
        )
        st.plotly_chart(fig_trend, use_container_width=True)

    # ── TAB4: ÖDEME TAKVİMİ ──────────────────────────────────────────────
    with tab4:
        st.subheader("📅 Aylık Ödeme Takvimi (Günlük Sıralı)")

        ay_sec_tak = st.selectbox(
            "Ay seçin",
            options=gelir_aylar,
            format_func=lambda x: x.capitalize(),
            index=gelir_aylar.index(bugun_ay) if bugun_ay in gelir_aylar else 0,
            key="tak_ay"
        )

        kk_tak = kk_proj_dict.get(ay_sec_tak, {})
        mun_tak = munferit_dict.get(ay_sec_tak, 0)
        df_tak = odeme_takvimi(ay_sec_tak, df_ihtiyac, df_khm, kk_tak, mun_tak)

        # Renk kodlaması
        TUR_RENK = {
            "İhtiyaç Kredisi": "#1f77b4",
            "KHM":             "#ff7f0e",
            "Kredi Kartı":     "#d62728",
            "Münferit":        "#9467bd",
        }

        toplam_odeme = df_tak["Tutar"].sum()
        st.markdown(f"**{ay_sec_tak.capitalize()} toplam ödeme: {para_fmt(toplam_odeme)}**")

        # Renkli tablo
        def renk_satir(row):
            renk = TUR_RENK.get(row["Tür"], "#555")
            return [f"background-color:{renk}22; border-left: 4px solid {renk}"] * len(row)

        show_tak = df_tak[["Gun","Tür","Açıklama","Tutar","Ödeme Tarihi"]].copy()
        show_tak["Tutar"] = show_tak["Tutar"].apply(para_fmt)
        show_tak.columns  = ["Gün","Tür","Açıklama","Tutar","Ödeme Tarihi"]

        st.dataframe(
            show_tak.style.apply(renk_satir, axis=1),
            use_container_width=True, hide_index=True
        )

        # Gantt benzeri görsel
        fig_tak = px.bar(
            df_tak.assign(Ay=ay_sec_tak.capitalize()),
            x="Tutar", y="Açıklama", orientation="h",
            color="Tür", color_discrete_map=TUR_RENK,
            text="Gun",
            title=f"{ay_sec_tak.capitalize()} – Günlük Ödeme Dağılımı",
            labels={"Tutar":"Tutar (₺)","Açıklama":""},
        )
        fig_tak.update_traces(texttemplate="%{text}. gün", textposition="inside")
        fig_tak.update_layout(height=500)
        st.plotly_chart(fig_tak, use_container_width=True)

    # ── TAB5: GENEL ANALİZ ───────────────────────────────────────────────
    with tab5:
        st.subheader("Genel Borç & Gelir Analizi")

        # Pasta: Borç dağılımı
        ozet_df = pd.DataFrame({
            "Borç Türü": ["İhtiyaç Kredisi","KHM","Kredi Kartı"],
            "Toplam Borç (₺)": [ihtiyac_borc, khm_borc, kk_borc],
        })
        fig_pie = px.pie(
            ozet_df, names="Borç Türü", values="Toplam Borç (₺)",
            title="Toplam Borç Dağılımı", hole=0.35,
            color_discrete_sequence=["#00CC96","#FFA15A","#EF553B"],
        )
        st.plotly_chart(fig_pie, use_container_width=True)

        st.markdown("---")

        # Aylık Gelir vs Gider (KK dinamik hesapla)
        st.markdown("#### 📅 Aylık Gelir & Gider Karşılaştırması")
        st.caption("Gider = İhtiyaç + KHM + KK min.ödeme (dinamik) + Münferit")

        aylik_rows = []
        for ay in gelir_aylar:
            gelir_val   = float(df_gelir.loc[df_gelir["ay"]==ay,"toplam_gelir"].values[0]) if ay in df_gelir["ay"].values else 0
            kk_ay       = kk_proj_dict.get(ay, {})
            kk_min      = sum(d["min_odeme"] for d in kk_ay.values())
            mun_val     = munferit_dict.get(ay, 0)
            gider_val   = ihtiyac_taksit + khm_taksit + kk_min + mun_val
            aylik_rows.append({
                "Ay":              ay.capitalize(),
                "Gelir":           gelir_val,
                "İhtiyaç Kredisi": ihtiyac_taksit,
                "KHM":             khm_taksit,
                "Kredi Kartı":     kk_min,
                "Münferit":        mun_val,
                "Toplam Gider":    gider_val,
                "Net":             gelir_val - gider_val,
            })

        df_aylik = pd.DataFrame(aylik_rows)

        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(name="Toplam Gelir", x=df_aylik["Ay"], y=df_aylik["Gelir"],
                                  marker_color="#00CC96",
                                  text=df_aylik["Gelir"].apply(lambda v: f"{v/1000:.0f}K"),
                                  textposition="outside"))
        fig_bar.add_trace(go.Bar(name="Toplam Gider", x=df_aylik["Ay"], y=df_aylik["Toplam Gider"],
                                  marker_color="#EF553B",
                                  text=df_aylik["Toplam Gider"].apply(lambda v: f"{v/1000:.0f}K"),
                                  textposition="outside"))
        fig_bar.update_layout(barmode="group", title="Aylık Gelir ve Gider",
                               yaxis_title="Tutar (₺)", height=420)
        st.plotly_chart(fig_bar, use_container_width=True)

        # Net çizgi
        fig_net = go.Figure()
        fig_net.add_trace(go.Scatter(
            x=df_aylik["Ay"], y=df_aylik["Net"],
            mode="lines+markers+text",
            line=dict(color="#636EFA", width=3),
            marker=dict(size=9),
            text=df_aylik["Net"].apply(lambda v: f"{v/1000:.0f}K ₺"),
            textposition="top center",
            fill="tozeroy", fillcolor="rgba(99,110,250,0.12)",
        ))
        fig_net.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
        fig_net.update_layout(title="Aylık Net Kalan (Gelir − Gider)",
                               yaxis_title="Net (₺)", height=340)
        st.plotly_chart(fig_net, use_container_width=True)

        # Detay tablosu
        st.markdown("#### 📋 Aylık Detay Tablosu")
        tablo = df_aylik.copy()
        for col in ["Gelir","İhtiyaç Kredisi","KHM","Kredi Kartı","Münferit","Toplam Gider","Net"]:
            tablo[col] = tablo[col].apply(para_fmt)
        st.dataframe(tablo, use_container_width=True, hide_index=True)

except Exception as e:
    st.error(f"Veri işlenirken hata oluştu: {e}")
    st.exception(e)
