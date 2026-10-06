"""Vector painter: CLIR -> HTML + inline SVG (CONTRACTS.md 7.1).

A card is a positioned box: shapes are inline SVG layers, text is absolutely
positioned HTML (real, selectable text) laid on the same baselines the layout
measured with HarfBuzz. Colours are CSS custom properties (--ni-<token>) so the
same markup serves both themes. Live bindings are resolved for the initial paint
at `now` (Python, live.apply_live) and then kept current by LIVE_JS, which mirrors
live.py's semantics exactly (tests/paint/test_js_parity.py checks this in a
headless browser when one is available).

`width` is the CONTENT-box width in CSS px, as for the raster painter.
No external assets: the Inter faces are embedded as data: URLs; images are
embedded (downscaled to their box at 2x).
"""
from __future__ import annotations

import base64
import html
import io
import json
from datetime import datetime
from functools import lru_cache

from .. import text as TX
from .. import tokens as TK
from . import live as LV
from . import shared as RS

PAD = TK.SPACE["pad"]


def _f(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _var(tok: str) -> str:
    return f"var(--ni-{tok})"


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


# --------------------------------------------------------------------------- css
@lru_cache(maxsize=1)
def font_faces() -> str:
    out = []
    for w in TX.WEIGHTS:
        b = base64.b64encode((TX.FONT_DIR / f"Inter-{w}.ttf").read_bytes()).decode()
        out.append(f'@font-face{{font-family:"NI Inter";font-weight:{w};font-style:normal;font-display:block;'
                   f'src:url(data:font/ttf;base64,{b}) format("truetype");}}')
    return "\n".join(out)


def theme_css() -> str:
    d, l = TK.css_vars("dark"), TK.css_vars("light")
    return (f":root,[data-theme=dark]{{\n{d}\n  color-scheme:dark;}}\n"
            f"[data-theme=light]{{\n{l}\n  color-scheme:light;}}\n"
            f"[data-ni-theme=dark]{{\n{d}\n}}\n[data-ni-theme=light]{{\n{l}\n}}\n"
            f".board[data-ni-theme]{{background:var(--ni-bg);box-sizing:content-box;padding:16px;border-radius:10px}}\n")


CARD_CSS = f"""
.ni-card{{position:relative;box-sizing:border-box;padding:{PAD - TK.SPACE['border_w']}px;border-radius:{TK.SPACE['radius']}px;
  border:{TK.SPACE['border_w']}px solid var(--ni-border);background:var(--ni-panel);overflow:hidden;
  font-family:{TX.CSS_FONT_STACK};font-kerning:normal;-webkit-font-smoothing:antialiased;
  text-rendering:geometricPrecision;flex:none}}
.ni-card .ni-c{{position:relative}}
.ni-card svg{{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}}
.ni-card .ni-t{{position:absolute;white-space:pre;overflow:hidden;text-overflow:ellipsis;unicode-bidi:isolate;margin:0}}
.ni-card .ni-t>span{{white-space:pre}}
.ni-card [data-hidden]{{display:none}}
"""


# --------------------------------------------------------------------------- prims
def _text_div(p: dict, x: float, lines: list[str], hidden: bool, z: int) -> str:
    px, wt, tnum, lh = RS.text_style(p)
    m = TX.metrics(px, wt)
    top0 = p["y"] - (lh - (m["ascent"] + m["descent"])) / 2 - m["ascent"]
    max_w = p.get("max_w") or 400
    direction = p.get("dir", "ltr")
    a = p.get("anchor", "start")
    phys = {"start": "end", "end": "start"}.get(a, a) if direction == "rtl" else a
    left = x if phys == "start" else x - max_w / 2 if phys == "middle" else x - max_w
    align = {"start": "left", "middle": "center", "end": "right"}[phys]
    feat = "'tnum' 1" if tnum else "normal"
    op = f"opacity:{p['alpha']};" if p.get("alpha", 1) != 1 else ""
    style = (f"left:{_f(left)}px;top:{_f(top0)}px;width:{_f(max_w)}px;font-size:{_f(px)}px;font-weight:{wt};"
             f"line-height:{_f(lh)}px;color:{_var(p['tok'])};text-align:{align};font-feature-settings:{feat};"
             f"z-index:{z};{op}")
    inner = "\n".join(
        f'<span data-hbw="{_f(TX.width(TX.clean(s), px, wt, tnum))}">{_esc(TX.clean(s))}</span>' for s in lines)
    hid = " data-hidden" if hidden else ""
    return (f'<div class="ni-t" data-p="{p["id"]}" data-k="{p["k"]}" dir="{direction}" '
            f'data-role="{p["role"]}"{hid} style="{style}">{inner}</div>')


def _svg_prim(p: dict, W: float, alpha_override=None) -> str:
    k = p["k"]
    rx = lambda a: LV.resolve_x(a, 0, W)
    op = p.get("alpha", 1)
    ops = f"opacity:{op};" if op != 1 else ""
    pid = f'data-p="{p["id"]}"'
    if k == "rect":
        x0, x1 = rx(p["x0"]), rx(p["x1"])
        fill = _var(p["tok"]) if p.get("tok") else "none"
        st = f"stroke:{_var(p['stroke'])};stroke-width:1;" if p.get("stroke") else ""
        return (f'<rect {pid} x="{_f(x0)}" y="{_f(p["y0"])}" width="{_f(max(0, x1 - x0))}" '
                f'height="{_f(max(0, p["y1"] - p["y0"]))}" rx="{_f(p.get("r", 0))}" style="fill:{fill};{st}{ops}"/>')
    if k == "line":
        dash = f"stroke-dasharray:{' '.join(_f(d) for d in p['dash'])};" if p.get("dash") else ""
        if p.get("gaps") and p.get("_segs") is not None:
            dash = f"stroke-dasharray:{' '.join(_f(d) for d in _gap_dash(p['_segs'], *sorted((p['y0'], p['y1']))))};"
        return (f'<line {pid} x1="{_f(rx(p["x0"]))}" y1="{_f(p["y0"])}" x2="{_f(rx(p["x1"]))}" y2="{_f(p["y1"])}" '
                f'style="stroke:{_var(p["tok"])};stroke-width:{_f(p.get("w", 1))};{dash}{ops}"/>')
    if k == "tri":
        pts = RS._tri_pts(rx(p["x"]), p["y"], p["size"], p["dir"])
        return f'<polygon {pid} points="{" ".join(f"{_f(a)},{_f(b)}" for a, b in pts)}" style="fill:{_var(p["tok"])};{ops}"/>'
    if k == "dot":
        x = rx(p["x"])
        ring = (f'<circle cx="{_f(x)}" cy="{_f(p["y"])}" r="{_f(p["r"] + p.get("ring_w", 0))}" '
                f'style="fill:{_var(p["ring"])}"/>') if p.get("ring") else ""
        return (f'<g {pid} style="{ops}">{ring}<circle cx="{_f(x)}" cy="{_f(p["y"])}" r="{_f(p["r"])}" '
                f'style="fill:{_var(p["tok"])}"/></g>')
    if k == "path":
        b = p["box"]
        x0, x1 = rx(b["x0"]), rx(b["x1"])
        pts = [(x0 + fx * (x1 - x0), b["y0"] + fy * (b["y1"] - b["y0"])) for fx, fy in p["pts"]]
        if len(pts) < 2:
            return ""
        d = "M" + " L".join(f"{_f(a)},{_f(c)}" for a, c in pts)
        out = ""
        if p.get("fill"):
            fd = d + f" L{_f(pts[-1][0])},{_f(b['y1'])} L{_f(pts[0][0])},{_f(b['y1'])} Z"
            out += f'<path d="{fd}" style="fill:{_var(p["fill"])};stroke:none"/>'
        dash = "stroke-dasharray:4 3;" if p.get("style") == "interp" else ""
        out += (f'<path d="{d}" style="fill:none;stroke:{_var(p["tok"])};stroke-width:{_f(p.get("w", 2))};'
                f'stroke-linejoin:round;stroke-linecap:round;{dash}"/>')
        return f'<g {pid} style="{ops}">{out}</g>'
    if k == "cells":
        b = p["box"]
        x0, x1 = rx(b["x0"]), rx(b["x1"])
        cols, rows, gap = p["cols"], p["rows"], p.get("gap", 1)
        cw, ch = (x1 - x0 - gap * (cols - 1)) / cols, (b["y1"] - b["y0"] - gap * (rows - 1)) / rows
        out = []
        steps = p.get("steps", 5)
        for i, v in enumerate(p["v"]):
            r_, c_ = divmod(i, cols)
            cx, cy = x0 + c_ * (cw + gap), b["y0"] + r_ * (ch + gap)
            if v is None or v < 0:
                st = f"fill:none;stroke:{_var('viz-grid')};stroke-width:1"
            elif p.get("ramp", "seq") == "seq":
                st = f"fill:var(--ni-viz-seq-{1 + round(v * 4 / max(1, steps - 1))})"
            else:
                t = v / max(1, steps - 1)
                if t <= 0.5:
                    st = f"fill:color-mix(in srgb,var(--ni-viz-div-mid) {_f(t * 200)}%,var(--ni-viz-div-neg))"
                else:
                    st = f"fill:color-mix(in srgb,var(--ni-viz-div-pos) {_f((t - .5) * 200)}%,var(--ni-viz-div-mid))"
            out.append(f'<rect x="{_f(cx)}" y="{_f(cy)}" width="{_f(cw)}" height="{_f(ch)}" rx="1.5" style="{st}"/>')
        return f'<g {pid} style="{ops}">{"".join(out)}</g>'
    if k == "image":
        b = p["box"]
        x0, x1 = rx(b["x0"]), rx(b["x1"])
        uri = _image_uri(p, max(1, round(x1 - x0)), max(1, round(b["y1"] - b["y0"])))
        cid = f"ni-clip-{p['id']}-{abs(hash((x0, x1, b['y0']))) % 10**8}"
        if not uri:
            return (f'<rect {pid} x="{_f(x0)}" y="{_f(b["y0"])}" width="{_f(x1 - x0)}" height="{_f(b["y1"] - b["y0"])}" '
                    f'rx="6" style="fill:{_var("viz-track")}"/>')
        par = "xMidYMid slice" if p.get("fit") == "cover" else "xMidYMid meet"
        return (f'<g {pid} style="{ops}"><clipPath id="{cid}"><rect x="{_f(x0)}" y="{_f(b["y0"])}" '
                f'width="{_f(x1 - x0)}" height="{_f(b["y1"] - b["y0"])}" rx="6"/></clipPath>'
                f'<image href="{uri}" x="{_f(x0)}" y="{_f(b["y0"])}" width="{_f(x1 - x0)}" '
                f'height="{_f(b["y1"] - b["y0"])}" preserveAspectRatio="{par}" clip-path="url(#{cid})"/></g>')
    if k == "basemap":
        b = p["box"]
        x0, x1 = rx(b["x0"]), rx(b["x1"])
        lon0, lat0, lon1, lat1 = p["bbox"]
        W2, H2 = x1 - x0, b["y1"] - b["y0"]
        ds = []
        for ring in RS.land()["polys"]:
            xs = [q[0] for q in ring]
            ys = [q[1] for q in ring]
            if max(xs) < lon0 or min(xs) > lon1 or max(ys) < lat0 or min(ys) > lat1:
                continue
            ds.append("M" + " L".join(f"{_f(x0 + (lo - lon0) / (lon1 - lon0) * W2)},"
                                      f"{_f(b['y0'] + (lat1 - la) / (lat1 - lat0) * H2)}" for lo, la in ring) + "Z")
        cid = f"ni-map-{p['id']}-{abs(hash((x0, x1, b['y0']))) % 10**8}"
        return (f'<g {pid} style="{ops}"><clipPath id="{cid}"><rect x="{_f(x0)}" y="{_f(b["y0"])}" width="{_f(W2)}" '
                f'height="{_f(H2)}"/></clipPath><path clip-path="url(#{cid})" d="{" ".join(ds)}" '
                f'style="fill:{_var(p.get("land", "map-land"))};stroke:{_var(p.get("stroke", "map-stroke"))};'
                f'stroke-width:0.75;stroke-linejoin:round"/></g>')
    if k == "icon":
        spec = RS.icons()
        kk = p["size"] / spec["box"]
        cx, cy = rx(p["x"]), p["y"]
        out = []
        for el in spec["icons"].get(p["name"], []):
            if el["t"] == "circle":
                (ex, ey), r = el["c"], el["r"]
                st = f"fill:{_var(p['tok'])}" if el.get("fill") else f"fill:none;stroke:{_var(p['tok'])}"
                out.append(f'<circle cx="{_f(ex)}" cy="{_f(ey)}" r="{_f(r)}" style="{st}"/>')
            else:
                pts = " ".join(f"{_f(x)},{_f(y)}" for x, y in el["pts"])
                tag = "polygon" if el.get("closed") else "polyline"
                st = f"fill:{_var(p['tok'])}" if el.get("fill") else f"fill:none;stroke:{_var(p['tok'])}"
                out.append(f'<{tag} points="{pts}" style="{st}"/>')
        return (f'<g {pid} transform="translate({_f(cx - 12 * kk)},{_f(cy - 12 * kk)}) scale({_f(kk)})" '
                f'style="stroke-width:{spec["stroke"]};stroke-linecap:round;stroke-linejoin:round;{ops}">'
                f'{"".join(out)}</g>')
    return ""


def _gap_dash(segs, ya, yb):
    """stroke-dasharray drawing only `segs` of a vertical line from ya to yb (mirrored in the client JS)."""
    arr, pos = [0.0], ya
    for s0, s1 in segs:
        arr += [s0 - pos, s1 - s0]
        pos = s1
    arr.append(yb - pos + 1000)
    return arr


def _image_uri(p: dict, bw: int, bh: int) -> str | None:
    path = RS.blob_path(p["ref"])
    if path is None:
        return None
    try:
        from PIL import Image
    except ImportError:  # the vector face stands the placeholder rect in; Pillow only paints image prims
        return None
    src = Image.open(path)
    try:
        src.seek(getattr(src, "n_frames", 1) - 1)
    except Exception:
        pass
    src = src.convert("RGB")
    if p.get("fit") == "cover" and p.get("crop"):
        fx0, fy0, fx1, fy1 = p["crop"]
        src = src.crop((round(fx0 * src.width), round(fy0 * src.height), round(fx1 * src.width), round(fy1 * src.height)))
    sc = min(1.0, max(2 * bw / src.width, 2 * bh / src.height))
    src = src.resize((max(1, round(src.width * sc)), max(1, round(src.height * sc))), Image.LANCZOS)
    buf = io.BytesIO()
    src.save(buf, format="JPEG", quality=84)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- live data for JS
def _live_payload(clir: dict, W: float) -> dict:
    by = {p["id"]: p for p in clir["prims"]}
    out = []
    for lb in clir.get("live", []):
        a = dict(lb["args"])
        if lb["k"] == "now_marker":
            b = a["box"]
            a["px"] = {"x0": LV.resolve_x(b["x0"], 0, W), "x1": LV.resolve_x(b["x1"], 0, W)}
            path = by.get(a.get("path"))
            if path:
                pb = path["box"]
                px0, px1 = LV.resolve_x(pb["x0"], 0, W), LV.resolve_x(pb["x1"], 0, W)
                a["curve"] = [[round(px0 + fx * (px1 - px0), 2), round(pb["y0"] + fy * (pb["y1"] - pb["y0"]), 2)]
                              for fx, fy in path["pts"]]
            a["kinds"] = {str(i): by[i]["k"] for i in a.get("prims", []) if i in by}
            if a.get("gaps"):
                from ..lint import text_box
                a["gapbox"] = [[round(v, 2) for v in text_box(by[i], W)[:4]] for i in a["gaps"] if i in by
                               and by[i]["k"] in ("text", "time")]
                ln = next((by[i] for i in a.get("prims", []) if i in by and by[i]["k"] == "line"), None)
                if ln is not None:
                    a["ly"] = sorted((ln["y0"], ln["y1"]))
        out.append({"k": lb["k"], "args": a})
    return {"live": out, "past_alpha": TK.VIZ["past_alpha"]}


# --------------------------------------------------------------------------- public
def card_html(clir: dict, *, width: int, theme: str, now: datetime, viewer_tz: str,
              card_key: str | None = None) -> str:
    """One card as a standalone fragment. theme: 'dark' | 'light' | 'auto' (follows the page)."""
    W = float(width)
    H = clir["bucket"]["h"]
    shown = LV.apply_live(clir, now, viewer_tz)
    sp = {p["id"]: p for p in shown["prims"]}
    parts: list[str] = []
    svg_buf: list[str] = []
    z = 1

    def flush():
        nonlocal z
        if svg_buf:
            parts.append(f'<svg width="{_f(W)}" height="{H}" viewBox="0 0 {_f(W)} {H}" style="z-index:{z}">'
                         + "".join(svg_buf) + "</svg>")
            svg_buf.clear()
            z += 1
    for p0 in clir["prims"]:
        p = sp.get(p0["id"], p0)                 # applied geometry/text; hidden prims keep their original
        hidden = p0["id"] not in sp
        if p["k"] in ("text", "time"):
            flush()
            x = LV.resolve_x(p["x"], 0, W)
            lines = p.get("lines", []) if p["k"] == "text" else [LV.time_prim_text(p, viewer_tz)]
            parts.append(_text_div(p, x, lines, hidden, z))
            z += 1
        else:
            if p["k"] == "line" and p.get("gaps"):
                p = dict(p, _segs=LV.line_segments(p, sp, W))
            s = _svg_prim(p, W)
            if hidden and s:
                s = s.replace(f'data-p="{p["id"]}"', f'data-p="{p["id"]}" data-hidden', 1)
            svg_buf.append(s)
    flush()
    th = f' data-ni-theme="{theme}"' if theme in ("dark", "light") else ""
    key = f' data-card="{_esc(card_key)}"' if card_key else ""
    live = ""
    if clir.get("live"):
        live = (f'<script type="application/json" class="ni-live">'
                f'{json.dumps(_live_payload(clir, W), separators=(",", ":")).replace("</", "<\\/")}</script>')
    aria = _esc(clir.get("summary", ""))
    return (f'<div class="ni-card"{th}{key} role="img" aria-label="{aria}" data-span="{_esc(clir.get("span", ""))}" '
            f'data-viewer-tz="{_esc(viewer_tz)}" style="width:{_f(W + 2 * PAD)}px;height:{_f(H + 2 * PAD)}px">'
            f'<div class="ni-c" style="width:{_f(W)}px;height:{H}px">{"".join(parts)}</div>{live}</div>')


LIVE_JS = r"""
(function(){
  "use strict";
  function secs(a,b){return (b-a)/1000;}
  function parseT(t){ if(t.length===10){var p=t.split('-');return Date.UTC(+p[0],+p[1]-1,+p[2]);} return Date.parse(t); }
  function countdown(s,fmt){
    s=Math.trunc(s); if(s<=0) return "now";
    var mt=Math.floor((s+59)/60), d=Math.floor(mt/1440), rem=mt%1440, h=Math.floor(rem/60), m=rem%60;
    if(fmt==="rel"||(fmt==="in_dhm"&&d>=2)){
      if(d>=2) return "in "+d+" days";
      if(d===1) return h===0?"in 1 day":"in 1 d "+h+" h";
    }
    h+=d*24;
    var core=(h&&m)?(h+" h "+m+" m"):(h?(h+" h"):(m+" min"));
    return fmt==="hm"?core:"in "+core;
  }
  function countUp(s,unit){
    s=Math.max(0,Math.trunc(s));
    if(unit==="auto"||!unit) unit=s>=2*86400?"day":(s>=2*3600?"hour":"minute");
    var n=Math.floor(s/({day:86400,hour:3600,minute:60})[unit]);
    return n.toLocaleString("en-US")+" "+unit+(n===1?"":"s");
  }
  function age(s){ s=Math.max(0,Math.trunc(s)); if(s<3600) return Math.max(1,Math.floor(s/60))+" min old";
    if(s<2*86400) return Math.floor(s/3600)+" h old"; return Math.floor(s/86400)+" d old"; }
  function num(v,fmt){
    var m=/^(,?)(?:\.(\d)([f%])|d)$/.exec(fmt||"")||[null,",","0","f"];
    var grp=m[1]===",", dp=m[2]===undefined?0:+m[2];
    if(m[3]==="%"){v=v*100;}
    var s=v.toLocaleString("en-US",{useGrouping:grp,minimumFractionDigits:dp,maximumFractionDigits:dp});
    return m[3]==="%"?s+"%":s;
  }
  function activeVariant(vs,now){
    var best=null;
    for(var i=0;i<vs.length;i++){
      var f=vs[i].t_from?parseT(vs[i].t_from):null, t=vs[i].t_to?parseT(vs[i].t_to):null;
      if((f===null||now>=f)&&(t===null||now<t)) return i;
      if(f===null||now>=f) best=i;
    }
    return best===null?0:best;
  }
  function el(card,id){return card.querySelector('[data-p="'+id+'"]');}
  function setText(card,id,s){var e=el(card,id); if(e){var sp=e.querySelector("span"); (sp||e).textContent=s;}}
  function curveY(curve,x){
    if(!curve||!curve.length) return null;
    if(x<=curve[0][0]) return curve[0][1];
    if(x>=curve[curve.length-1][0]) return curve[curve.length-1][1];
    for(var i=1;i<curve.length;i++){var a=curve[i-1],b=curve[i];
      if(x>=a[0]&&x<=b[0]) return b[0]===a[0]?a[1]:a[1]+(b[1]-a[1])*(x-a[0])/(b[0]-a[0]);}
    return null;
  }
  function moveTo(e,kind,x,y,a){
    if(!e) return;
    if(kind==="line"){e.setAttribute("x1",x);e.setAttribute("x2",x);
      if(a&&a.gapbox&&a.ly){var ya=a.ly[0],yb=a.ly[1],segs=[[ya,yb]];
        a.gapbox.forEach(function(b){ if(x<b[0]-3||x>b[2]+3) return; var c0=b[1]-2,c1=b[3]+2,n=[];
          segs.forEach(function(s){ if(c1<=s[0]||c0>=s[1]){n.push(s);return;} if(c0>s[0]) n.push([s[0],c0]); if(c1<s[1]) n.push([c1,s[1]]); });
          segs=n; });
        var arr=[0],pos=ya; segs.forEach(function(s){ if(s[1]-s[0]>1){arr.push(s[0]-pos,s[1]-s[0]);pos=s[1];} }); arr.push(yb-pos+1000);
        e.style.strokeDasharray=arr.join(" ");}}
    else if(kind==="dot"){e.querySelectorAll("circle").forEach(function(c){c.setAttribute("cx",x); if(y!==null) c.setAttribute("cy",y);});}
    else if(kind==="text"||kind==="time"){e.style.transform="translateX(0)";}
  }
  function hide(e,on){ if(!e) return; if(on) e.setAttribute("data-hidden",""); else e.removeAttribute("data-hidden"); }
  function apply(card,data,now){
    var shown={}, hidden={};
    data.live.forEach(function(lb){
      var a=lb.args;
      if(lb.k==="now_marker"){
        var t0=parseT(a.t0),t1=parseT(a.t1),frac=(t1>t0)?(now-t0)/(t1-t0):-1;
        (a.prims||[]).forEach(function(id){
          var e=el(card,id); if(!e) return;
          if(frac<0||frac>1){hidden[id]=1;return;}
          var x=a.px.x0+(a.px.x1-a.px.x0)*frac, y=a.curve?curveY(a.curve,x):null;
          moveTo(e,a.kinds[String(id)],x,y,a);
        });
      } else if(lb.k==="countdown"){ setText(card,a.prim,countdown(secs(now,parseT(a.t)),a.fmt||"in_hm")); }
      else if(lb.k==="count_up"){ setText(card,a.prim,countUp(secs(parseT(a.t0),now),a.unit||"auto")); }
      else if(lb.k==="age"){ setText(card,a.prim,age(secs(parseT(a.t),now))); }
      else if(lb.k==="extrapolate"){ setText(card,a.prim,num(a.v0+a.rate_per_s*secs(parseT(a.t0),now),a.fmt||",.0f")); }
      else if(lb.k==="timed_variants"){
        var vs=a.variants||[]; if(!vs.length) return; var act=activeVariant(vs,now);
        vs.forEach(function(v,i){(v.prims||[]).forEach(function(id){ if(i===act) shown[id]=1; else hidden[id]=1; });});
      } else if(lb.k==="past_dim"){
        var past=now>=parseT(a.t);
        (a.prims||[]).forEach(function(id){var e=el(card,id); if(e) e.style.opacity=past?String(data.past_alpha):"";});
      }
    });
    Object.keys(hidden).forEach(function(id){ if(!shown[id]) hide(el(card,id),true); });
    Object.keys(shown).forEach(function(id){ hide(el(card,id),false); });
  }
  function nowMs(){ return (typeof window.NI_NOW==="number")?window.NI_NOW:Date.now(); }
  function tick(){
    document.querySelectorAll(".ni-card").forEach(function(card){
      var s=card.querySelector("script.ni-live"); if(!s) return;
      if(!card._ni) card._ni=JSON.parse(s.textContent);
      apply(card,card._ni,nowMs());
    });
  }
  window.NI_LIVE={tick:tick,countdown:countdown,countUp:countUp,age:age,num:num,activeVariant:activeVariant,parseT:parseT};
  tick(); setInterval(tick,15000);
})();
"""


PAGE_JS = r"""
(function(){
  var b=document.getElementById("ni-theme");
  if(b) b.addEventListener("click",function(){
    var r=document.documentElement, t=r.getAttribute("data-theme")==="light"?"dark":"light";
    r.setAttribute("data-theme",t); b.textContent=t==="light"?"Dark theme":"Light theme";
    try{localStorage.setItem("ni-theme",t);}catch(e){}
  });
  try{var t=localStorage.getItem("ni-theme"); if(t&&b){document.documentElement.setAttribute("data-theme",t);
    b.textContent=t==="light"?"Dark theme":"Light theme";}}catch(e){}
  var c=document.getElementById("ni-clock");
  if(c) c.addEventListener("change",function(){ if(c.checked){window._niFrozen=window.NI_NOW; window.NI_NOW=undefined;}
    else {window.NI_NOW=window._niFrozen;} if(window.NI_LIVE) window.NI_LIVE.tick(); });
})();
"""


def page(cards: list[dict], *, title: str, body_html: str | None = None, now: datetime | None = None,
         extra_css: str = "", clock_toggle: bool = False) -> str:
    """Standalone page. `cards` = [{"html": card_html(...), "caption": str?}] laid in a wrapping row, unless
    `body_html` is given (then the caller owns the layout). `now` freezes the live clock (NI_NOW)."""
    body = body_html if body_html is not None else (
        '<div class="ni-row">' + "".join(
            f'<figure>{c["html"]}<figcaption>{_esc(c.get("caption", ""))}</figcaption></figure>' for c in cards)
        + "</div>")
    frozen = f"<script>window.NI_NOW={int(now.timestamp() * 1000)};</script>" if now else ""
    clock = ('<label class="ni-clockl"><input type="checkbox" id="ni-clock"> Run the clock live</label>'
             if clock_toggle else "")
    return f"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
{font_faces()}
{theme_css()}
html,body{{margin:0;background:var(--ni-bg);color:var(--ni-text);font-family:{TX.CSS_FONT_STACK}}}
{CARD_CSS}
.ni-row{{display:flex;flex-wrap:wrap;gap:16px;padding:16px;align-items:flex-start}}
figure{{margin:0}} figcaption{{font-size:12px;color:var(--ni-muted);margin-top:6px;max-width:100%}}
.ni-top{{display:flex;gap:12px;align-items:center;justify-content:flex-end;padding:12px 16px 0}}
.ni-top button{{font:inherit;font-size:13px;background:var(--ni-panel);color:var(--ni-text);border:1px solid var(--ni-border);
  border-radius:8px;padding:6px 10px;cursor:pointer}}
.ni-clockl{{font-size:13px;color:var(--ni-muted)}}
{extra_css}
</style></head><body>
<div class="ni-top">{clock}<button id="ni-theme" type="button">Light theme</button></div>
{body}
{frozen}
<script>{LIVE_JS}</script>
<script>{PAGE_JS}</script>
</body></html>"""
