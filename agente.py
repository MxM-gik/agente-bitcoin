import ccxt
import time
import threading
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timedelta

# ============================================
# AGENTE MULTI-MERCADO — ESTRATEGIA BUY THE DIP
# Mercados: BTC, ETH, BNB
# ============================================

CAPITAL_INICIAL = 15000   # CLP total
CLP_POR_USD     = 950

# Reglas de entrada
CAIDA_LEVE   = 0.01
CAIDA_MEDIA  = 0.03
CAIDA_FUERTE = 0.05
CAPITAL_CAIDA_LEVE   = 0.30
CAPITAL_CAIDA_MEDIA  = 0.60
CAPITAL_CAIDA_FUERTE = 0.89

# Reglas de salida
SALIDA_MINIMA = 0.005
SALIDA_MEDIA  = 0.015
SALIDA_FUERTE = 0.03

COMISION               = 0.001
STOP_LOSS_DIARIO       = 0.20
STOP_LOSS_CATASTROFICO = 0.18
CAIDA_REACTIVA         = 0.02

# Mercados disponibles (símbolo, nombre, ícono, color)
MERCADOS_CONFIG = [
    {"simbolo": "BTC/USDT", "nombre": "Bitcoin",  "icono": "₿", "color": "#ff9500"},
    {"simbolo": "ETH/USDT", "nombre": "Ethereum", "icono": "Ξ", "color": "#00cfff"},
    {"simbolo": "BNB/USDT", "nombre": "BNB",      "icono": "◈", "color": "#ffe600"},
]

# Capital inicial por mercado (proporcional)
CAPITAL_POR_MERCADO = {
    "BTC/USDT": int(CAPITAL_INICIAL * 0.50),   # 7,500
    "ETH/USDT": int(CAPITAL_INICIAL * 0.30),   # 4,500
    "BNB/USDT": int(CAPITAL_INICIAL * 0.20),   # 3,000
}

exchange = ccxt.binance()

# ── TELEGRAM ──
TELEGRAM_TOKEN   = "8722467841:AAENHyHPMxFkWZSuBkqUFRKMqraM7Vbmcwg"
TELEGRAM_CHAT_ID = "1523499171"

def telegram(msg):
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                                 "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

# ── PALETA ──
BG         = "#050510"
BG2        = "#0a0a1a"
BG3        = "#0f0f22"
NARANJA    = "#ff9500"
VERDE_NEON = "#00ff88"
ROJO_NEON  = "#ff2052"
AZUL_NEON  = "#00cfff"
AZUL_ELEC  = "#0099ff"
AMARILLO   = "#ffe600"
BLANCO     = "#ffffff"
TEXTO_DIM  = "#8899aa"
FUENTE     = "Segoe UI"

# ============================================
# ESTADO POR MERCADO
# ============================================

class MercadoState:
    def __init__(self, cfg):
        self.simbolo   = cfg["simbolo"]
        self.nombre    = cfg["nombre"]
        self.icono     = cfg["icono"]
        self.color     = cfg["color"]

        cap = CAPITAL_POR_MERCADO[self.simbolo]
        self.capital_disponible  = cap
        self.capital_inicial     = cap
        self.ganancia_total      = 0
        self.operaciones         = 0
        self.capital_recuperado  = False

        self.posiciones          = []
        self.precio_base         = 0
        self.precio_actual       = 0
        self.historial_precios   = []
        self.buffer_lecturas     = []
        self.log_lecturas        = []
        self.log_transacciones   = []
        self.pausado_hasta       = None
        self.precio_ultima_venta = 0
        self.compras_consecutivas= 0
        self.precio_max_dia      = 0
        self.precio_min_dia      = 0
        self.ultimo_ciclo_ts     = None
        self.loop_running        = False

# Crear estado para cada mercado
estados = [MercadoState(cfg) for cfg in MERCADOS_CONFIG]
agente_activo = True

# ============================================
# FUNCIONES CORE
# ============================================

def clp_a_usd(clp): return clp / CLP_POR_USD
def usd_a_clp(usd): return usd * CLP_POR_USD

def btc_total(e):    return sum(p['btc'] for p in e.posiciones)
def valor_pos_clp(e, precio): return usd_a_clp(btc_total(e) * precio)

def ganancia_pct(e, precio):
    if not e.posiciones: return 0
    total_inv = sum(p['capital_invertido_clp'] for p in e.posiciones)
    if total_inv == 0: return 0
    return (valor_pos_clp(e, precio) - total_inv) / total_inv

def log_mercado(e, msg):
    ts = datetime.now().strftime('%H:%M:%S')
    e.log_lecturas.insert(0, f"[{ts}]  {msg}")
    if len(e.log_lecturas) > 200: e.log_lecturas.pop()
    print(f"[{ts}][{e.nombre}] {msg}")

def log_tx(e, msg):
    ts = datetime.now().strftime('%H:%M:%S')
    e.log_transacciones.insert(0, f"[{ts}]  {msg}")
    if len(e.log_transacciones) > 100: e.log_transacciones.pop()
    log_mercado(e, f"★ {msg}")

def simular_compra(e, precio, porcentaje_capital, motivo):
    if e.capital_disponible <= 0:
        log_mercado(e, "Sin capital disponible.")
        return
    monto_clp    = e.capital_disponible * porcentaje_capital
    monto_usd    = clp_a_usd(monto_clp)
    com_usd      = monto_usd * COMISION
    activo_comp  = (monto_usd - com_usd) / precio
    e.capital_disponible -= monto_clp
    e.posiciones.append({
        'btc': activo_comp,
        'precio_compra': precio,
        'capital_invertido_clp': monto_clp,
        'ciclos': 0
    })
    e.compras_consecutivas += 1
    msg = f"COMPRA {motivo} | ${precio:,.2f} USD | ${monto_clp:,.0f} CLP"
    log_tx(e, msg)
    telegram(f"{e.icono} <b>COMPRA — {e.nombre}</b>\n💵 ${precio:,.2f} USD\n💰 ${monto_clp:,.0f} CLP\n📌 {motivo}")

def simular_venta_parcial(e, precio, porcentaje, motivo, actualizar_base=False):
    if not e.posiciones: return
    btc_v       = btc_total(e) * porcentaje
    valor_usd   = btc_v * precio
    com_usd     = valor_usd * COMISION
    neto_clp    = usd_a_clp(valor_usd - com_usd)
    total_inv   = sum(p['capital_invertido_clp'] for p in e.posiciones)
    ganancia    = neto_clp - (total_inv * porcentaje)
    e.ganancia_total      += ganancia
    e.capital_disponible  += neto_clp
    e.operaciones         += 1
    e.precio_ultima_venta  = precio
    for p in e.posiciones:
        p['btc']                  *= (1 - porcentaje)
        p['capital_invertido_clp'] *= (1 - porcentaje)
    e.posiciones[:] = [p for p in e.posiciones if p['btc'] > 0.000001]
    if e.capital_disponible >= e.capital_inicial and not e.capital_recuperado:
        e.capital_recuperado = True
        log_tx(e, "*** CAPITAL RECUPERADO — FASE 2 ACTIVADA ***")
    signo = "+" if ganancia >= 0 else ""
    log_tx(e, f"VENTA {motivo} | ${precio:,.2f} USD | {porcentaje*100:.0f}% | {signo}${ganancia:,.0f} CLP")
    emoji = "💰" if ganancia > 0 else "🔴"
    telegram(f"{emoji} <b>VENTA — {e.nombre}</b>\n💵 ${precio:,.2f} USD\n{signo}${ganancia:,.0f} CLP\n📌 {motivo}")
    e.compras_consecutivas = 0
    if actualizar_base:
        manana = datetime.now() + timedelta(days=1)
        e.pausado_hasta = manana.replace(hour=8, minute=0, second=0, microsecond=0)
        e.precio_base   = 0
        log_mercado(e, f"PAUSA hasta {e.pausado_hasta.strftime('%d/%m %H:%M')}")

def tendencia_confirmada(e):
    if len(e.buffer_lecturas) < 3:
        return False, "Acumulando lecturas..."
    sobre  = sum(1 for p in e.buffer_lecturas if p >= e.precio_base)
    bajo   = sum(1 for p in e.buffer_lecturas if p <  e.precio_base)
    subiendo = e.buffer_lecturas[-1] > e.buffer_lecturas[0]
    if bajo == 0 and sobre >= 2 and subiendo:
        return True, f"Tendencia confirmada ({len(e.buffer_lecturas)} lecturas)"
    elif bajo > 0:
        return False, f"{bajo} lectura(s) bajo la base"
    else:
        return False, f"{sobre}/{len(e.buffer_lecturas)} sobre base — esperando"

def puede_vender(e, precio, porcentaje):
    """Valida que la venta tenga sentido antes de ejecutarla."""
    total_inv = sum(p['capital_invertido_clp'] for p in e.posiciones)
    inv_a_vender = total_inv * porcentaje
    # Regla 1: lo invertido a vender debe ser > 1% del capital inicial
    if inv_a_vender <= e.capital_inicial * 0.01:
        log_mercado(e, f"VENTA BLOQUEADA — posición muy pequeña (${inv_a_vender:,.0f} CLP)")
        return False
    # Regla 2: ganancia debe superar 20 CLP
    btc_v      = btc_total(e) * porcentaje
    neto_clp   = usd_a_clp(btc_v * precio * (1 - COMISION))
    ganancia   = neto_clp - inv_a_vender
    if ganancia < 20:
        log_mercado(e, f"VENTA BLOQUEADA — ganancia insuficiente (${ganancia:,.0f} CLP < $20)")
        return False
    return True

def obtener_precio(e):
    for intento in range(3):
        try:
            ticker = exchange.fetch_ticker(e.simbolo)
            return ticker['last']
        except Exception:
            if intento < 2:
                time.sleep(10)
    raise Exception(f"{e.nombre}: Binance no respondió")

# ============================================
# LOOP POR MERCADO
# ============================================

def loop_mercado(e):
    e.loop_running = True
    capital_inicio_dia = e.capital_disponible
    log_mercado(e, f"Agente {e.nombre} iniciado — estableciendo precio base...")
    telegram(f"🤖 <b>AGENTE {e.nombre.upper()} INICIADO</b>\nCapital: ${e.capital_disponible:,.0f} CLP")

    while agente_activo:
        try:
            # ── PAUSA ──
            if e.pausado_hasta and datetime.now() < e.pausado_hasta:
                precio = obtener_precio(e)
                e.precio_actual = precio
                e.historial_precios.append(precio)
                if len(e.historial_precios) > 60: e.historial_precios.pop(0)
                if e.precio_ultima_venta > 0:
                    caida = (e.precio_ultima_venta - precio) / e.precio_ultima_venta
                    if caida >= CAIDA_REACTIVA:
                        e.pausado_hasta = None
                        e.buffer_lecturas.clear()
                        log_mercado(e, f"PAUSA CANCELADA — cayó {caida*100:.2f}% desde venta")
                        telegram(f"⚡ <b>PAUSA CANCELADA — {e.nombre}</b>\nCaída {caida*100:.2f}% desde última venta")
                        continue
                log_mercado(e, f"En pausa | ${precio:,.2f} USD")
                time.sleep(300)
                continue

            elif e.pausado_hasta and datetime.now() >= e.pausado_hasta:
                e.pausado_hasta   = None
                e.precio_base     = 0
                e.compras_consecutivas = 0
                e.buffer_lecturas.clear()
                e.historial_precios.clear()
                log_mercado(e, "Pausa terminada — reiniciando")

            # ── LECTURA ──
            precio = obtener_precio(e)
            e.precio_actual   = precio
            e.ultimo_ciclo_ts = datetime.now()
            e.historial_precios.append(precio)
            if len(e.historial_precios) > 60: e.historial_precios.pop(0)

            if precio > e.precio_max_dia or e.precio_max_dia == 0: e.precio_max_dia = precio
            if precio < e.precio_min_dia or e.precio_min_dia == 0: e.precio_min_dia = precio

            # ── PRECIO BASE ──
            if e.precio_base == 0:
                e.precio_base = precio
                e.buffer_lecturas.clear()
                log_mercado(e, f"Precio base: ${precio:,.2f} USD")
                telegram(f"📌 <b>BASE {e.nombre}</b>: ${precio:,.2f} USD")
                time.sleep(300)
                continue

            precio_anterior = e.historial_precios[-2] if len(e.historial_precios) >= 2 else precio
            precio_bajando  = precio < precio_anterior

            e.buffer_lecturas.append(precio)
            if len(e.buffer_lecturas) > 4: e.buffer_lecturas.pop(0)

            variacion = (precio - e.precio_base) / e.precio_base
            caida     = abs(variacion) if variacion < 0 else 0
            subida    = variacion       if variacion > 0 else 0

            for p in e.posiciones: p['ciclos'] += 1

            # ── SALIDA — solo vende si hay ganancia real y precio empieza a bajar ──
            if e.posiciones:
                g = ganancia_pct(e, precio)
                perdida_max = min(
                    (p['precio_compra'] - precio) / p['precio_compra']
                    for p in e.posiciones
                )
                if perdida_max >= STOP_LOSS_CATASTROFICO:
                    simular_venta_parcial(e, precio, 1.0, f"CATASTRÓFICO -{perdida_max*100:.1f}%")
                elif precio_bajando and g >= SALIDA_FUERTE:
                    if puede_vender(e, precio, 0.90):
                        simular_venta_parcial(e, precio, 0.90, f"Pico +{g*100:.2f}% >=3%", actualizar_base=True)
                elif precio_bajando and g >= SALIDA_MEDIA:
                    if puede_vender(e, precio, 0.90):
                        simular_venta_parcial(e, precio, 0.90, f"Pico +{g*100:.2f}% >=1.5%", actualizar_base=True)
                elif precio_bajando and g >= SALIDA_MINIMA:
                    if puede_vender(e, precio, 0.50):
                        simular_venta_parcial(e, precio, 0.50, f"Pico +{g*100:.2f}% >=0.5%")
                elif precio_bajando and g < SALIDA_MINIMA:
                    log_mercado(e, f"PRECIO BAJA — ganancia insuficiente {g*100:.3f}% — manteniendo")
                else:
                    log_mercado(e, f"MANTENIENDO {g*100:.3f}% | ${precio:,.2f} | esperando pico")

            # ── ENTRADA — compra cuando rebota, usando caída del precio anterior (el piso) ──
            precio_subiendo  = precio > precio_anterior
            caida_anterior   = max(0, (e.precio_base - precio_anterior) / e.precio_base) if e.precio_base > 0 else 0

            if e.capital_disponible > 0:
                if precio_subiendo and caida_anterior >= CAIDA_LEVE:
                    # Rebote confirmado — usar caída del piso para decidir cuánto comprar
                    if e.compras_consecutivas >= 2:
                        log_mercado(e, f"ESPERANDO — 2 compras seguidas | piso -{caida_anterior*100:.2f}%")
                        e.compras_consecutivas = max(0, e.compras_consecutivas - 1)
                    elif caida_anterior >= CAIDA_FUERTE:
                        simular_compra(e, precio, CAPITAL_CAIDA_FUERTE, f"rebote desde piso -{caida_anterior*100:.2f}% >=5%")
                    elif caida_anterior >= CAIDA_MEDIA:
                        simular_compra(e, precio, CAPITAL_CAIDA_MEDIA,  f"rebote desde piso -{caida_anterior*100:.2f}% >=3%")
                    else:
                        simular_compra(e, precio, CAPITAL_CAIDA_LEVE,   f"rebote desde piso -{caida_anterior*100:.2f}% >=1%")
                elif not precio_subiendo and caida >= CAIDA_LEVE:
                    log_mercado(e, f"PRECIO SIGUE BAJANDO {caida*100:.3f}% — esperando rebote")
                elif not e.posiciones:
                    log_mercado(e, f"ESPERANDO  ${precio:,.2f} | caída {caida*100:.3f}% insuficiente | base ${e.precio_base:,.2f}")

            # Stop loss diario
            cap_total = e.capital_disponible + valor_pos_clp(e, precio)
            perdida_dia = (capital_inicio_dia - cap_total) / capital_inicio_dia if capital_inicio_dia > 0 else 0
            if perdida_dia >= STOP_LOSS_DIARIO:
                log_mercado(e, f"STOP LOSS DIARIO {perdida_dia*100:.1f}% — pausa 30 min")
                time.sleep(1800)
                capital_inicio_dia = cap_total

            time.sleep(300)

        except Exception as ex:
            log_mercado(e, f"Error — reintentando en 30s ({str(ex)[:50]})")
            time.sleep(30)

    e.loop_running = False


def watchdog():
    while agente_activo:
        time.sleep(60)
        for e in estados:
            if e.ultimo_ciclo_ts:
                mins = (datetime.now() - e.ultimo_ciclo_ts).seconds / 60
                if mins > 10 and not e.loop_running:
                    log_mercado(e, f"WATCHDOG — {mins:.0f} min sin lectura. Reiniciando hilo...")
                    telegram(f"⚠️ <b>{e.nombre}</b> — {mins:.0f} min sin lectura. Reiniciando...")
                    hilo = threading.Thread(target=loop_mercado, args=(e,), daemon=True)
                    hilo.start()


# ============================================
# UI — COMPONENTES
# ============================================

def neon_button_info(parent, linea1, linea2, color_neon, command):
    outer = tk.Frame(parent, bg=color_neon, padx=1, pady=1)
    inner = tk.Frame(outer, bg=BG2)
    inner.pack(fill="both", expand=True)
    frame_texto = tk.Frame(inner, bg=BG2, pady=6, padx=8)
    frame_texto.pack(fill="both", expand=True)
    lbl1 = tk.Label(frame_texto, text=linea1, font=(FUENTE, 9, "bold"),
                    fg=color_neon, bg=BG2, anchor="w")
    lbl1.pack(fill="x")
    lbl2 = tk.Label(frame_texto, text=linea2, font=(FUENTE, 7),
                    fg=AMARILLO, bg=BG2, anchor="w")
    lbl2.pack(fill="x")

    def on_enter(ev):
        inner.config(bg=color_neon); frame_texto.config(bg=color_neon)
        lbl1.config(bg=color_neon, fg=BG); lbl2.config(bg=color_neon, fg=BG)
    def on_leave(ev):
        inner.config(bg=BG2); frame_texto.config(bg=BG2)
        lbl1.config(bg=BG2, fg=color_neon); lbl2.config(bg=BG2, fg=AMARILLO)

    for w in [inner, frame_texto, lbl1, lbl2]:
        w.bind("<Enter>", on_enter); w.bind("<Leave>", on_leave)
        w.bind("<Button-1>", lambda ev: command())

    outer._lbl1 = lbl1; outer._lbl2 = lbl2
    return outer


def neon_button(parent, text, color_neon, command):
    outer = tk.Frame(parent, bg=color_neon, padx=1, pady=1)
    inner = tk.Frame(outer, bg=BG2)
    inner.pack(fill="both", expand=True)
    btn = tk.Button(inner, text=text, font=(FUENTE, 9, "bold"),
                    fg=color_neon, bg=BG2, activeforeground=BG,
                    activebackground=color_neon, relief="flat", bd=0,
                    pady=8, padx=6, cursor="hand2", command=command)
    btn.pack(fill="both", expand=True)
    btn.bind("<Enter>", lambda e: btn.config(bg=color_neon, fg=BG))
    btn.bind("<Leave>", lambda e: btn.config(bg=BG2, fg=color_neon))
    return outer


class Grafico:
    def __init__(self, parent, color, width=480, height=80):
        self.width   = width
        self.height  = height
        self.color   = color
        self.canvas  = tk.Canvas(parent, width=width, height=height,
                                 bg=BG3, highlightthickness=0)
        self.canvas.pack(fill="x")

    def dibujar(self, precios, precio_base_val):
        c = self.canvas; c.delete("all")
        if len(precios) < 2:
            c.create_text(self.width//2, self.height//2,
                          text="Acumulando datos...", fill=TEXTO_DIM, font=(FUENTE, 9))
            return
        w, h, pad = self.width, self.height, 10
        mn = min(precios); mx = max(precios); rng = mx - mn if mx != mn else 1
        def xp(i): return pad + (i/(len(precios)-1))*(w-pad*2)
        def yp(v): return h - pad - ((v-mn)/rng)*(h-pad*2)
        if precio_base_val and mn <= precio_base_val <= mx:
            yb = yp(precio_base_val)
            c.create_line(pad, yb, w-pad, yb, fill=AMARILLO, width=1, dash=(4,4))
            c.create_text(w-pad-2, yb-6, text=f"Base ${precio_base_val:,.0f}",
                          fill=AMARILLO, font=(FUENTE, 7), anchor="e")
        pts = [(xp(i), yp(p)) for i, p in enumerate(precios)]
        color_l = VERDE_NEON if precios[-1] >= precios[0] else ROJO_NEON
        for i in range(len(pts)-1):
            c.create_line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1],
                          fill=color_l, width=2)
        px, py = pts[-1]
        py_t = max(pad+8, min(py, h-pad-8))
        c.create_oval(px-4, py-4, px+4, py+4, fill=color_l, outline="")
        c.create_text(px-6, py_t, text=f"${precios[-1]:,.0f}",
                      fill=color_l, font=(FUENTE, 8, "bold"), anchor="e")


# ============================================
# INTERFAZ PRINCIPAL
# ============================================

class InterfazAgente:
    def __init__(self, root):
        self.root = root
        self.root.title("Agente Multi-Mercado  //  Simulación")
        self.root.geometry("820x740")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.mercado_idx = 0          # Índice del mercado visible
        self.ultimo_tx_count  = -1
        self.ultimo_lec_count = -1
        self._build()
        self.actualizar_ui()

    def estado_actual(self):
        return estados[self.mercado_idx]

    def _build(self):
        root = self.root

        # ── HEADER ──
        hdr = tk.Frame(root, bg=BG, pady=6)
        hdr.pack(fill="x", padx=16)
        tk.Label(hdr, text="⬡  AGENTE MULTI-MERCADO",
                 font=(FUENTE, 15, "bold"), bg=BG, fg=NARANJA).pack(side="left")
        rh = tk.Frame(hdr, bg=BG)
        rh.pack(side="right")
        tk.Label(rh, text="SIMULACIÓN", font=(FUENTE, 7), bg=BG, fg=TEXTO_DIM).pack(anchor="e")
        self.lbl_hora = tk.Label(rh, text="", font=(FUENTE, 10, "bold"), bg=BG, fg=AZUL_NEON)
        self.lbl_hora.pack(anchor="e")
        tk.Frame(root, bg=NARANJA, height=2).pack(fill="x")

        # ── SELECTOR DE MERCADO ──
        self.tab_frame = tk.Frame(root, bg=BG, pady=6)
        self.tab_frame.pack(fill="x", padx=16)
        self.tab_btns = []
        for i, e in enumerate(estados):
            idx = i
            btn = tk.Button(self.tab_frame,
                            text=f"{e.icono}  {e.nombre}",
                            font=(FUENTE, 10, "bold"),
                            fg=e.color, bg=BG2,
                            activeforeground=BG,
                            activebackground=e.color,
                            relief="flat", bd=0,
                            padx=14, pady=6,
                            cursor="hand2",
                            command=lambda i=idx: self.cambiar_mercado(i))
            btn.pack(side="left", padx=(0,4))
            self.tab_btns.append(btn)

        # ── KPI TOTAL (suma todos los mercados) ──
        fkt = tk.Frame(root, bg=BG3, padx=10, pady=4)
        fkt.pack(fill="x", padx=16, pady=(0,4))
        tk.Label(fkt, text="PORTAFOLIO TOTAL:", font=(FUENTE, 7),
                 bg=BG3, fg=TEXTO_DIM).pack(side="left")
        self.lbl_total_global = tk.Label(fkt, text="$-- CLP",
                                         font=(FUENTE, 9, "bold"), bg=BG3, fg=VERDE_NEON)
        self.lbl_total_global.pack(side="left", padx=8)
        self.lbl_gan_global = tk.Label(fkt, text="Ganancia: $-- CLP",
                                       font=(FUENTE, 9, "bold"), bg=BG3, fg=NARANJA)
        self.lbl_gan_global.pack(side="left", padx=8)

        tk.Frame(root, bg=TEXTO_DIM, height=1).pack(fill="x", padx=16, pady=2)

        # ── PRECIO + GRÁFICO ──
        fila_top = tk.Frame(root, bg=BG)
        fila_top.pack(fill="x", padx=16, pady=4)

        fp = tk.Frame(fila_top, bg=BG2, padx=12, pady=8)
        fp.pack(side="left", fill="y")
        self.lbl_precio = tk.Label(fp, text="$--.-- USD",
                                   font=(FUENTE, 20, "bold"), bg=BG2, fg=VERDE_NEON)
        self.lbl_precio.pack(anchor="w")

        def pill(parent, lbl_txt, color):
            f = tk.Frame(parent, bg=BG2); f.pack(anchor="w")
            tk.Label(f, text=lbl_txt, font=(FUENTE, 8), bg=BG2, fg=TEXTO_DIM).pack(side="left")
            lbl = tk.Label(f, text="--", font=(FUENTE, 8, "bold"), bg=BG2, fg=color)
            lbl.pack(side="left", padx=(2,0))
            return lbl

        self.lbl_base  = pill(fp, "Base:",    BLANCO)
        self.lbl_caida = pill(fp, "Caída:",   ROJO_NEON)
        self.lbl_gpos  = pill(fp, "Posición:",VERDE_NEON)

        fg = tk.Frame(fila_top, bg=BG3, padx=4, pady=4)
        fg.pack(side="left", fill="both", expand=True, padx=(8,0))
        self.lbl_grafico_titulo = tk.Label(fg, text="",
                                           font=(FUENTE, 7), bg=BG3, fg=TEXTO_DIM)
        self.lbl_grafico_titulo.pack(anchor="w")
        self.grafico = Grafico(fg, VERDE_NEON, width=480, height=80)

        tk.Frame(root, bg=TEXTO_DIM, height=1).pack(fill="x", padx=16, pady=2)

        # ── KPIs DEL MERCADO ACTUAL ──
        fk = tk.Frame(root, bg=BG)
        fk.pack(fill="x", padx=16, pady=2)
        def kpi(parent, titulo, color):
            f = tk.Frame(parent, bg=BG3, padx=8, pady=5)
            f.pack(side="left", expand=True, fill="x", padx=2)
            tk.Label(f, text=titulo, font=(FUENTE, 7), bg=BG3, fg=TEXTO_DIM).pack()
            lbl = tk.Label(f, text="$--", font=(FUENTE, 9, "bold"), bg=BG3, fg=color)
            lbl.pack()
            return lbl
        self.lbl_libre     = kpi(fk, "CAPITAL LIBRE",  AZUL_NEON)
        self.lbl_invertido = kpi(fk, "EN INVERSIÓN",   AMARILLO)
        self.lbl_total     = kpi(fk, "CAPITAL TOTAL",  AZUL_NEON)
        self.lbl_ganancia  = kpi(fk, "GANANCIA",       NARANJA)

        fi = tk.Frame(root, bg=BG)
        fi.pack(fill="x", padx=16, pady=2)
        self.lbl_info = tk.Label(fi, text="", font=(FUENTE, 7), bg=BG, fg=TEXTO_DIM)
        self.lbl_info.pack(side="left")
        self.lbl_dia = tk.Label(fi, text="", font=(FUENTE, 7), bg=BG, fg=TEXTO_DIM)
        self.lbl_dia.pack(side="right")

        tk.Frame(root, bg=TEXTO_DIM, height=1).pack(fill="x", padx=16, pady=2)

        # ── PANEL INFERIOR: BOTONES + LOGS ──
        panel = tk.Frame(root, bg=BG)
        panel.pack(fill="both", expand=True, padx=16, pady=(0,8))

        col_ctrl = tk.Frame(panel, bg=BG)
        col_ctrl.pack(side="left", fill="y", padx=(0,8))

        tk.Label(col_ctrl, text="COMPRAR", font=(FUENTE, 8, "bold"),
                 bg=BG, fg=VERDE_NEON).pack(anchor="w", pady=(0,2))
        self.btns_compra = {}
        for pct, txt in [(0.30,"30%"),(0.60,"60%"),(0.89,"89%")]:
            btn = neon_button_info(col_ctrl, txt, "→ $-- CLP",
                                   VERDE_NEON, lambda p=pct: self.compra_manual(p))
            btn.pack(fill="x", pady=2)
            self.btns_compra[pct] = btn

        tk.Frame(col_ctrl, bg=AZUL_ELEC, height=1).pack(fill="x", pady=5)

        tk.Label(col_ctrl, text="VENDER", font=(FUENTE, 8, "bold"),
                 bg=BG, fg=ROJO_NEON).pack(anchor="w", pady=(0,2))
        self.btns_venta = {}
        for pct, txt in [(0.50,"50%"),(0.90,"90%")]:
            btn = neon_button_info(col_ctrl, txt, "→ Sin posición",
                                   ROJO_NEON, lambda p=pct: self.venta_manual(p))
            btn.pack(fill="x", pady=2)
            self.btns_venta[pct] = btn

        tk.Frame(col_ctrl, bg=TEXTO_DIM, height=1).pack(fill="x", pady=5)

        fila_base = tk.Frame(col_ctrl, bg=BG)
        fila_base.pack(fill="x", pady=2)
        neon_button(fila_base, "Reset base", AZUL_NEON,
                    self.reset_base).pack(side="left", expand=True, fill="x", padx=(0,2))
        neon_button(fila_base, "＋ Capital", AZUL_NEON,
                    self.agregar_capital).pack(side="left", expand=True, fill="x", padx=(2,0))

        col_logs = tk.Frame(panel, bg=BG)
        col_logs.pack(side="left", fill="both", expand=True)

        frame_prox = tk.Frame(col_logs, bg=BG3, pady=4, padx=8)
        frame_prox.pack(fill="x", pady=(0,4))
        fila_px = tk.Frame(frame_prox, bg=BG3)
        fila_px.pack(fill="x")
        tk.Label(fila_px, text="PRÓXIMA ACCIÓN:",
                 font=(FUENTE, 7), bg=BG3, fg=TEXTO_DIM).pack(side="left")
        self.lbl_prox = tk.Label(fila_px, text="Analizando...",
                                 font=(FUENTE, 8, "bold"), bg=BG3, fg=AMARILLO)
        self.lbl_prox.pack(side="left", padx=(4,0))
        self.lbl_timer = tk.Label(fila_px, text="",
                                  font=(FUENTE, 8, "bold"), bg=BG3, fg=AZUL_NEON)
        self.lbl_timer.pack(side="right")

        tk.Label(col_logs, text="⚡  TRANSACCIONES",
                 font=(FUENTE, 8, "bold"), bg=BG, fg=AZUL_NEON).pack(anchor="w")
        self.log_tx_box = tk.Text(col_logs, font=("Consolas", 7),
                                  bg=BG3, fg=AZUL_NEON, relief="flat", bd=0,
                                  height=6, state="disabled")
        self.log_tx_box.pack(fill="x", pady=(2,5))
        self.log_tx_box.bind("<MouseWheel>", lambda e: self.log_tx_box.yview_scroll(int(-1*(e.delta/120)), "units"))
        self.log_tx_box.bind("<Button-4>",   lambda e: self.log_tx_box.yview_scroll(-1, "units"))
        self.log_tx_box.bind("<Button-5>",   lambda e: self.log_tx_box.yview_scroll( 1, "units"))

        tk.Label(col_logs, text="○  LECTURAS DE MERCADO",
                 font=(FUENTE, 8, "bold"), bg=BG, fg=BLANCO).pack(anchor="w")
        self.log_mkt_box = tk.Text(col_logs, font=("Consolas", 7),
                                   bg=BG2, fg=BLANCO, relief="flat", bd=0,
                                   state="disabled")
        self.log_mkt_box.pack(fill="both", expand=True, pady=(2,0))
        self.log_mkt_box.bind("<MouseWheel>", lambda e: self.log_mkt_box.yview_scroll(int(-1*(e.delta/120)), "units"))
        self.log_mkt_box.bind("<Button-4>",   lambda e: self.log_mkt_box.yview_scroll(-1, "units"))
        self.log_mkt_box.bind("<Button-5>",   lambda e: self.log_mkt_box.yview_scroll( 1, "units"))

    # ── CAMBIAR MERCADO ──
    def cambiar_mercado(self, idx):
        self.mercado_idx = idx
        self.ultimo_tx_count  = -1
        self.ultimo_lec_count = -1
        # Actualizar tabs (resaltar activo)
        for i, btn in enumerate(self.tab_btns):
            e = estados[i]
            btn.config(bg=e.color if i == idx else BG2,
                       fg=BG       if i == idx else e.color)

    # ── ACCIONES ──
    def compra_manual(self, pct):
        e = self.estado_actual()
        if e.precio_actual == 0:
            messagebox.showwarning("Espera", "Sin precio base aún.")
            return
        if e.capital_disponible <= 0:
            messagebox.showwarning("Sin capital", "No hay capital disponible.")
            return
        simular_compra(e, e.precio_actual, pct, f"MANUAL {pct*100:.0f}%")

    def venta_manual(self, pct):
        e = self.estado_actual()
        if not e.posiciones:
            messagebox.showwarning("Sin posición", f"No hay {e.nombre} en cartera.")
            return
        simular_venta_parcial(e, e.precio_actual, pct,
                              f"MANUAL {pct*100:.0f}%", actualizar_base=False)

    def reset_base(self):
        e = self.estado_actual()
        if e.precio_actual == 0: return
        e.precio_base = e.precio_actual
        log_mercado(e, f"BASE ACTUALIZADA: ${e.precio_base:,.2f} USD")

    def agregar_capital(self):
        e = self.estado_actual()
        ventana = tk.Toplevel(self.root)
        ventana.title(f"Agregar capital — {e.nombre}")
        ventana.geometry("340x180")
        ventana.configure(bg=BG)
        ventana.resizable(False, False)
        tk.Label(ventana, text=f"¿Cuánto CLP agregar a {e.nombre}?",
                 font=(FUENTE, 11, "bold"), bg=BG, fg=e.color).pack(pady=(20,8))
        entry = tk.Entry(ventana, font=(FUENTE, 14, "bold"),
                         bg=BG2, fg=BLANCO, insertbackground=BLANCO,
                         relief="flat", bd=4, justify="center")
        entry.pack(padx=30, fill="x"); entry.focus()

        def confirmar():
            try:
                monto = float(entry.get().replace(",","").replace(".",""))
                if monto <= 0: raise ValueError
                e.capital_disponible += monto
                log_mercado(e, f"CAPITAL AGREGADO: +${monto:,.0f} CLP")
                telegram(f"💵 <b>CAPITAL {e.nombre}</b>\n+${monto:,.0f} CLP")
                ventana.destroy()
            except ValueError:
                tk.Label(ventana, text="Ingresa un número válido",
                         font=(FUENTE, 9), bg=BG, fg=ROJO_NEON).pack()

        neon_button(ventana, "Confirmar", VERDE_NEON, confirmar).pack(pady=12, padx=30, fill="x")

    # ── ACTUALIZAR UI ──
    def actualizar_ui(self):
        try:
            e = self.estado_actual()
            precio    = e.precio_actual
            cap_total = e.capital_disponible + valor_pos_clp(e, precio) if precio > 0 else e.capital_disponible
            caida     = max(0, (e.precio_base - precio) / e.precio_base * 100) if e.precio_base > 0 and precio > 0 else 0
            g_pos     = ganancia_pct(e, precio) * 100 if e.posiciones and precio > 0 else 0

            # Hora
            self.lbl_hora.config(text=datetime.now().strftime("%d/%m/%Y   %H:%M:%S"))

            # Portafolio global
            total_global = sum(
                est.capital_disponible + valor_pos_clp(est, est.precio_actual)
                for est in estados if est.precio_actual > 0
            )
            gan_global = sum(est.ganancia_total for est in estados)
            self.lbl_total_global.config(text=f"${total_global:,.0f} CLP")
            self.lbl_gan_global.config(
                text=f"Ganancia total: ${gan_global:,.0f} CLP",
                fg=VERDE_NEON if gan_global >= 0 else ROJO_NEON)

            # Tabs — resaltar activo
            for i, btn in enumerate(self.tab_btns):
                est = estados[i]
                activo = (i == self.mercado_idx)
                btn.config(bg=est.color if activo else BG2,
                           fg=BG       if activo else est.color)

            # Precio
            self.lbl_precio.config(
                text=f"{e.icono}  ${precio:,.2f} USD",
                fg=VERDE_NEON if precio >= e.precio_base else ROJO_NEON)
            self.lbl_grafico_titulo.config(text=f"{e.nombre} — Últimos ciclos")
            self.lbl_base.config(text=f"${e.precio_base:,.2f} USD")
            self.lbl_caida.config(
                text=f"{caida:.3f}%",
                fg=ROJO_NEON if caida >= 1.0 else (AMARILLO if caida >= 0.5 else BLANCO))
            self.lbl_gpos.config(
                text=f"{g_pos:+.2f}%",
                fg=VERDE_NEON if g_pos >= 0 else ROJO_NEON)

            # KPIs
            en_inv = valor_pos_clp(e, precio) if precio > 0 else 0
            self.lbl_libre.config(text=f"${e.capital_disponible:,.0f} CLP")
            self.lbl_invertido.config(text=f"${en_inv:,.0f} CLP",
                                      fg=AMARILLO if en_inv > 0 else TEXTO_DIM)
            self.lbl_total.config(text=f"${cap_total:,.0f} CLP")
            self.lbl_ganancia.config(
                text=f"${e.ganancia_total:,.0f} CLP",
                fg=VERDE_NEON if e.ganancia_total >= 0 else ROJO_NEON)

            self.lbl_info.config(
                text=f"Posiciones: {len(e.posiciones)}   Ops: {e.operaciones}   "
                     f"Fase: {'2' if e.capital_recuperado else '1'}")
            if e.precio_max_dia > 0:
                self.lbl_dia.config(
                    text=f"Máx: ${e.precio_max_dia:,.0f}   Mín: ${e.precio_min_dia:,.0f}")

            # Próxima acción
            if e.pausado_hasta and datetime.now() < e.pausado_hasta:
                self.lbl_prox.config(
                    text=f"En pausa hasta {e.pausado_hasta.strftime('%H:%M')} — vigilando caída 2%",
                    fg=AMARILLO)
            elif e.precio_base == 0:
                self.lbl_prox.config(text="Estableciendo precio base...", fg=TEXTO_DIM)
            elif e.posiciones:
                p15 = e.precio_base * (1 + SALIDA_MEDIA)
                p30 = e.precio_base * (1 + SALIDA_FUERTE)
                self.lbl_prox.config(
                    text=f"Vende 1.5% → ${p15:,.0f}  |  3% → ${p30:,.0f}  |  Ahora: {g_pos:+.2f}%",
                    fg=VERDE_NEON if g_pos >= 0 else ROJO_NEON)
            else:
                p1 = e.precio_base * (1 - CAIDA_LEVE)
                p3 = e.precio_base * (1 - CAIDA_MEDIA)
                self.lbl_prox.config(
                    text=f"Compra 30% si baja a ${p1:,.0f}  |  60% si baja a ${p3:,.0f}",
                    fg=AZUL_NEON)

            # Timer
            if e.ultimo_ciclo_ts:
                seg = max(0, 300 - (datetime.now() - e.ultimo_ciclo_ts).seconds)
                self.lbl_timer.config(text=f"Próx: {seg//60}:{seg%60:02d}")

            # Gráfico
            self.grafico.dibujar(e.historial_precios, e.precio_base)

            # Botones compra
            for pct, btn in self.btns_compra.items():
                monto = e.capital_disponible * pct
                btn._lbl2.config(text=f"→ ${monto:,.0f} CLP", fg=AMARILLO)

            # Botones venta
            for pct, btn in self.btns_venta.items():
                if e.posiciones and precio > 0:
                    btc_v   = btc_total(e) * pct
                    bruto   = usd_a_clp(btc_v * precio) * (1 - COMISION)
                    inv     = sum(p['capital_invertido_clp'] for p in e.posiciones) * pct
                    gan     = bruto - inv
                    signo   = "+" if gan >= 0 else ""
                    color   = VERDE_NEON if gan >= 0 else ROJO_NEON
                    txt     = f"→ {signo}${gan:,.0f} CLP ({signo}{gan/inv*100:.2f}%)" if inv > 0 else "→ --"
                    btn._lbl2.config(text=txt, fg=color)
                else:
                    btn._lbl2.config(text="→ Sin posición activa", fg=TEXTO_DIM)

            # Logs
            if len(e.log_transacciones) != self.ultimo_tx_count:
                self.log_tx_box.config(state="normal")
                self.log_tx_box.delete("1.0","end")
                for l in e.log_transacciones[:20]:
                    self.log_tx_box.insert("end", l + "\n")
                self.log_tx_box.config(state="disabled")
                self.ultimo_tx_count = len(e.log_transacciones)

            if len(e.log_lecturas) != self.ultimo_lec_count:
                self.log_mkt_box.config(state="normal")
                self.log_mkt_box.delete("1.0","end")
                for l in e.log_lecturas[:40]:
                    self.log_mkt_box.insert("end", l + "\n")
                self.log_mkt_box.config(state="disabled")
                self.ultimo_lec_count = len(e.log_lecturas)

        except Exception:
            pass

        self.root.after(3000, self.actualizar_ui)


# ============================================
# MAIN
# ============================================

if __name__ == "__main__":
    # Iniciar un hilo por mercado
    for e in estados:
        hilo = threading.Thread(target=loop_mercado, args=(e,), daemon=True)
        hilo.start()
        time.sleep(5)   # Escalonar arranque para no saturar Binance

    # Watchdog
    hilo_wd = threading.Thread(target=watchdog, daemon=True)
    hilo_wd.start()

    # UI
    root = tk.Tk()
    app = InterfazAgente(root)
    root.mainloop()
