"""Export the PagoTotal Intelligence architecture as SVG, PNG, PDF, and Mermaid."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.lib.utils import ImageReader

OUT = Path("docs")
W, H, SCALE = 1600, 1000, 1
NAVY, BLUE, TEAL, AMBER, RED, GREEN, INK, MUTED, LINE, BG, WHITE = (
    "#101828", "#2457A6", "#087E8B", "#B54708", "#B42318", "#027A48", "#182230", "#667085", "#D0D5DD", "#F8FAFC", "#FFFFFF"
)

def box(draw, xy, title, subtitle, fill, border=LINE):
    x, y, w, h = xy
    draw.rounded_rectangle((x, y, x+w, y+h), radius=16, fill=fill, outline=border, width=2)
    draw.text((x+22, y+18), title, font=F_BOLD, fill=INK)
    yy = y+55
    for line in subtitle.split("\n"):
        draw.text((x+22, yy), line, font=F_SMALL, fill=MUTED)
        yy += 25

def arrow(draw, start, end, color=BLUE, label=None):
    x1, y1 = start; x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=4)
    dx, dy = x2-x1, y2-y1
    if abs(dx) >= abs(dy):
        points = [(x2, y2), (x2-14 if dx > 0 else x2+14, y2-8), (x2-14 if dx > 0 else x2+14, y2+8)]
    else:
        points = [(x2, y2), (x2-8, y2-14 if dy > 0 else y2+14), (x2+8, y2-14 if dy > 0 else y2+14)]
    draw.polygon(points, fill=color)
    if label:
        mx, my = (x1+x2)/2, (y1+y2)/2
        draw.text((mx+8, my-22), label, font=F_TINY, fill=color)

def make_png(path):
    global F_TITLE, F_SUB, F_BOLD, F_SMALL, F_TINY
    image = Image.new("RGB", (W, H), BG); draw = ImageDraw.Draw(image)
    font = "C:/Windows/Fonts/arial.ttf"; bold = "C:/Windows/Fonts/arialbd.ttf"
    F_TITLE = ImageFont.truetype(bold, 38); F_SUB = ImageFont.truetype(font, 18)
    F_BOLD = ImageFont.truetype(bold, 20); F_SMALL = ImageFont.truetype(font, 16); F_TINY = ImageFont.truetype(font, 14)
    draw.text((70, 45), "PagoTotal Intelligence | Architecture", font=F_TITLE, fill=NAVY)
    draw.text((72, 95), "Local hackathon deployment: payment monitoring, diagnosis, control APIs, and dashboard", font=F_SUB, fill=MUTED)
    draw.line((70, 132, 1530, 132), fill=LINE, width=2)

    # Source and analytical runtime
    box(draw, (70, 205, 300, 160), "Mock transaction stream", "generator.py\nScenarios + live trial injections\nmerchant | provider | method | country | bank", "#EAF2FF", "#A9C7F8")
    box(draw, (450, 175, 470, 220), "Live dashboard engine", "live_dashboard.py\nRolling detector + time-aware baseline\nHierarchical diagnosis + cost/recovery\nPrioritization + incident memory match", "#E8F7F5", "#9ADBD4")
    box(draw, (1000, 205, 300, 160), "Dashboard snapshot", "frontend/dashboard_data.json\nKPIs | incidents | evidence | chart\nrewritten every refresh cycle", "#F2F4F7")
    arrow(draw, (370, 285), (450, 285))
    arrow(draw, (920, 285), (1000, 285))

    # Storage lane
    draw.text((70, 465), "Local operational data", font=F_BOLD, fill=NAVY)
    box(draw, (70, 505, 255, 138), "Historical baseline", "data/history.jsonl\nNormal transactions\nweekday/hour expectations", "#FFFFFF")
    box(draw, (360, 505, 255, 138), "Live state", "live_transactions.jsonl\nactive_incident_costs.json\npriorities.json", "#FFFFFF")
    box(draw, (650, 505, 255, 138), "Memory and controls", "incident_memory.json\nruntime_config.json\nlive_injections.json", "#FFFFFF")
    arrow(draw, (197, 505), (560, 395), color=TEAL)
    arrow(draw, (487, 505), (650, 395), color=TEAL)
    arrow(draw, (777, 505), (760, 395), color=TEAL)

    # Control API and front-end
    box(draw, (1030, 505, 310, 138), "Control server API", "control_server.py\n/api/config | /api/trial-injections\n/api/chat (optional OpenAI)", "#FFF7E8", "#F6D7A7")
    box(draw, (1130, 740, 330, 152), "Operations dashboard", "PagoTotal-Intelligence_1.html\nPolls snapshot + uses control APIs\nCommand Center | Explorer | Memory", "#EEF4FF", "#B8D2F8")
    # Keep external edges outside other components so the data flow stays readable.
    draw.line((1300, 285, 1410, 285, 1410, 700, 1295, 700, 1295, 740), fill=BLUE, width=4)
    draw.polygon([(1295, 740), (1287, 726), (1303, 726)], fill=BLUE)
    draw.line((1185, 643, 1185, 740), fill=AMBER, width=4)
    draw.polygon([(1185, 740), (1177, 726), (1193, 726)], fill=AMBER)

    # Optional external service
    box(draw, (70, 740, 300, 152), "Optional OpenAI service", "Used only for safe explanation/chat\nReceives calculated aggregates, not\nraw payment streams", "#FDF2FA", "#F4B8DA")
    draw.line((1030, 575, 960, 575, 960, 700, 370, 700, 370, 815), fill=AMBER, width=4)
    draw.polygon([(370, 815), (362, 801), (378, 801)], fill=AMBER)

    draw.text((450, 790), "Human-in-the-loop boundary", font=F_BOLD, fill=RED)
    draw.text((450, 825), "The platform detects, diagnoses, explains, and recommends. It does not execute routing changes.", font=F_SMALL, fill=MUTED)
    draw.rounded_rectangle((440, 765, 1020, 870), radius=14, outline="#F2C6C2", width=2)
    image.save(path)

def make_svg(path):
    # Embed the PNG as a fallback-free vector-compatible image only for raster consumers is avoided;
    # SVG stays fully editable with basic shapes and text.
    def rect(x,y,w,h,fill,stroke): return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    def text(x,y,value,size=16,weight=400,fill=INK):
        chunks = value.split("\n"); return "".join(f'<text x="{x}" y="{y+i*25}" font-family="Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}">{line}</text>' for i,line in enumerate(chunks))
    def arr(x1,y1,x2,y2,label="",color=BLUE):
        return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="4" marker-end="url(#arrow)"/>{text((x1+x2)/2+8,(y1+y2)/2-14,label,14,400,color) if label else ""}'
    items = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000">',
        f'<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="{BLUE}"/></marker></defs>',
        f'<rect width="1600" height="1000" fill="{BG}"/>', text(70,82,"PagoTotal Intelligence | Architecture",38,700,NAVY), text(72,120,"Local hackathon deployment: payment monitoring, diagnosis, control APIs, and dashboard",18,400,MUTED), f'<line x1="70" y1="132" x2="1530" y2="132" stroke="{LINE}" stroke-width="2"/>'
    ]
    nodes = [
        (70,205,300,160,"Mock transaction stream","generator.py\nScenarios + live trial injections\nmerchant | provider | method | country | bank","#EAF2FF","#A9C7F8"),
        (450,175,470,220,"Live dashboard engine","live_dashboard.py\nRolling detector + time-aware baseline\nHierarchical diagnosis + cost/recovery\nPrioritization + incident memory match","#E8F7F5","#9ADBD4"),
        (1000,205,300,160,"Dashboard snapshot","frontend/dashboard_data.json\nKPIs | incidents | evidence | chart\nrewritten every refresh cycle","#F2F4F7",LINE),
        (70,505,255,138,"Historical baseline","data/history.jsonl\nNormal transactions\nweekday/hour expectations",WHITE,LINE),
        (360,505,255,138,"Live state","live_transactions.jsonl\nactive_incident_costs.json\npriorities.json",WHITE,LINE),
        (650,505,255,138,"Memory and controls","incident_memory.json\nruntime_config.json\nlive_injections.json",WHITE,LINE),
        (1030,505,310,138,"Control server API","control_server.py\n/api/config | /api/trial-injections\n/api/chat (optional OpenAI)","#FFF7E8","#F6D7A7"),
        (1130,740,330,152,"Operations dashboard","PagoTotal-Intelligence_1.html\nPolls snapshot + uses control APIs\nCommand Center | Explorer | Memory","#EEF4FF","#B8D2F8"),
        (70,740,300,152,"Optional OpenAI service","Used only for safe explanation/chat\nReceives calculated aggregates, not\nraw payment streams","#FDF2FA","#F4B8DA")]
    for x,y,w,h,title,sub,fill,stroke in nodes: items += [rect(x,y,w,h,fill,stroke), text(x+22,y+42,title,20,700), text(x+22,y+80,sub,16,400,MUTED)]
    items += [arr(370,285,450,285),arr(920,285,1000,285),text(70,480,"Local operational data",20,700,NAVY),arr(197,505,560,395,"",TEAL),arr(487,505,650,395,"",TEAL),arr(777,505,760,395,"",TEAL), f'<path d="M1300 285 H1410 V700 H1295 V740" fill="none" stroke="{BLUE}" stroke-width="4" marker-end="url(#arrow)"/>', f'<path d="M1185 643 V740" fill="none" stroke="{AMBER}" stroke-width="4" marker-end="url(#arrow)"/>', f'<path d="M1030 575 H960 V700 H370 V815" fill="none" stroke="{AMBER}" stroke-width="4" marker-end="url(#arrow)"/>', f'<rect x="440" y="765" width="580" height="105" rx="14" fill="none" stroke="#F2C6C2" stroke-width="2"/>', text(450,805,"Human-in-the-loop boundary",20,700,RED), text(450,842,"The platform recommends actions; it never executes routing changes.",16,400,MUTED), '</svg>']
    path.write_text("\n".join(items), encoding="utf-8")

def make_pdf(png, path):
    c = canvas.Canvas(str(path), pagesize=(W*0.45, H*0.45))
    c.drawImage(ImageReader(str(png)), 0, 0, width=W*0.45, height=H*0.45)
    c.save()

def make_mermaid(path):
    path.write_text("""flowchart LR
  G[Mock transaction stream\ngenerator.py] --> E[Live dashboard engine\nlive_dashboard.py]
  H[data/history.jsonl\nHistorical baseline] --> E
  C[data/runtime_config.json\nincident_memory.json\nlive_injections.json] --> E
  E --> L[data/live_transactions.jsonl\nactive incident costs]
  E --> S[frontend/dashboard_data.json]
  S --> D[Operations dashboard\nHTML + JavaScript]
  A[control_server.py\nConfig, trials, chat API] --> D
  A --> C
  A -. optional, server-side .-> O[OpenAI API\nExplanation and chat]
""", encoding="utf-8")

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    png = OUT / "PagoTotal_Intelligence_Architecture.png"
    make_png(png)
    make_svg(OUT / "PagoTotal_Intelligence_Architecture.svg")
    make_pdf(png, OUT / "PagoTotal_Intelligence_Architecture.pdf")
    make_mermaid(OUT / "PagoTotal_Intelligence_Architecture.mmd")
    print("architecture exports created")
