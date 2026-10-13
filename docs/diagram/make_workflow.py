"""PPT용 Workflow 비교 다이어그램(SVG) 생성: python docs/diagram/make_workflow.py"""
from pathlib import Path

W, H = 1600, 900
FONT = "'Apple SD Gothic Neo','Pretendard','Noto Sans KR','Malgun Gothic',sans-serif"
INK, MUTED, FAINT = "#2a2019", "#7a6b60", "#b3a69a"
OR = "#ff6f0f"
GR = "#9a8b7e"            # 사람 단계
PANEL_B, PANEL_A = "#f7f2ed", "#ffffff"

ROWS = [
    {"no": "01", "title": "규정 질문", "y": 182,
     "stat": ("3", "1", "사람이 하는 단계", "답변 대기 → 즉시 답변"),
     "before": ["인사담당자에게|메신저·전화로 문의", "담당자가 규정을|찾아 해석", "답변 전달|신입은 그동안 대기"],
     "before_actor": ["사람"] * 3,
     "after": ["채팅으로 질문", "AI가 조문 검색|· 재순위", "근거 조문 + 쉬운 말로|즉시 답변", "근거 없으면|담당자 연결 (예외)"],
     "after_actor": ["사람", "AI", "AI", "AI"]},
    {"no": "02", "title": "회의 후속 업무", "y": 490,
     "stat": ("5", "2", "사람이 하는 단계", "정리·등록·독촉 → 업로드 + 승인"),
     "before": ["회의록 읽고|할 일 추리기", "담당자·기한|표로 정리", "캘린더에|하나씩 등록", "Slack 공유", "기한 확인·|독촉"],
     "before_actor": ["사람"] * 5,
     "after": ["회의록 업로드", "AI가 요약·할 일|담당자·기한 추출", "미확정 채우고|승인", "Slack 일정 공유|+ 캘린더 파일", "마감 전날·당일·|기한 지남 재알림"],
     "after_actor": ["사람", "AI", "사람", "자동", "자동"]},
]
PX0, PX1 = 48, 1296           # 레인 패널 가로
SX0, SX1 = 214, 1272          # 스텝 영역
LANE_H, LANE_GAP = 124, 12
SX = 1320                     # 오른쪽 통계 카드
out = []
a = out.append
a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}">')
a('<defs>'
  '<filter id="sh" x="-10%" y="-20%" width="120%" height="150%"><feDropShadow dx="0" dy="8" stdDeviation="12" flood-color="#ff6f0f" flood-opacity=".12"/></filter>'
  '<filter id="sh2" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="4" stdDeviation="8" flood-color="#5a3a1e" flood-opacity=".08"/></filter>'
  f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fffbf7"/><stop offset="1" stop-color="#fff4ea"/></linearGradient>'
  '</defs>')
a(f'<rect width="{W}" height="{H}" fill="url(#bg)"/>')
# 헤더
a(f'<text x="48" y="58" font-size="15" font-weight="800" fill="{OR}" letter-spacing="4">ORIENT · WORKFLOW</text>')
a(f'<text x="48" y="104" font-size="40" font-weight="800" fill="{INK}" letter-spacing="-0.5">AI 적용 전 vs 후</text>')
a(f'<text x="48" y="132" font-size="18" fill="{MUTED}">같은 업무에서 사람이 하는 단계는 줄고, 사람은 검토·승인과 예외에만 개입합니다</text>')
# 범례
a(f'<circle cx="1392" cy="84" r="7" fill="{GR}"/><text x="1406" y="90" font-size="15" fill="{MUTED}">사람</text>')
a(f'<circle cx="1466" cy="84" r="7" fill="{OR}"/><text x="1480" y="90" font-size="15" fill="{MUTED}">AI · 자동</text>')

def lane(y, steps, actors, before):
    panel = PANEL_B if before else PANEL_A
    if before:
        a(f'<rect x="{PX0}" y="{y}" width="{PX1-PX0}" height="{LANE_H}" rx="22" fill="{panel}"/>')
    else:
        a(f'<rect x="{PX0}" y="{y}" width="{PX1-PX0}" height="{LANE_H}" rx="22" fill="{panel}" stroke="#ffd7b8" stroke-width="1.5" filter="url(#sh)"/>')
        a(f'<rect x="{PX0}" y="{y+24}" width="5" height="{LANE_H-48}" rx="2.5" fill="{OR}"/>')
    # 레인 라벨
    a(f'<text x="{PX0+34}" y="{y+LANE_H/2-2}" font-size="21" font-weight="800" fill="{INK if not before else MUTED}">{"적용 전" if before else "적용 후"}</text>')
    a(f'<text x="{PX0+34}" y="{y+LANE_H/2+22}" font-size="13.5" fill="{FAINT if before else OR}" font-weight="{500 if before else 700}">{"사람이 직접" if before else "ORIENT"}</text>')
    n = len(steps); span = (SX1 - SX0) / n
    cy = y + 40
    cxs = [SX0 + span * (i + .5) for i in range(n)]
    col = "#d8cdc2" if before else OR
    a(f'<line x1="{cxs[0]:.1f}" y1="{cy}" x2="{cxs[-1]+span/2-8:.1f}" y2="{cy}" stroke="{col}" stroke-width="{2.5 if before else 3}" stroke-linecap="round"/>')
    ax = cxs[-1] + span / 2 - 8
    a(f'<path d="M{ax-9:.1f} {cy-7} L{ax+3:.1f} {cy} L{ax-9:.1f} {cy+7}" fill="none" stroke="{col}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
    for i, (txt, actor) in enumerate(zip(steps, actors)):
        cx = cxs[i]; fill = GR if actor == "사람" else OR
        a(f'<circle cx="{cx:.1f}" cy="{cy}" r="19" fill="#fff" stroke="{fill if before is False or actor!="사람" else "#d8cdc2"}" stroke-width="2.5"/>')
        a(f'<circle cx="{cx:.1f}" cy="{cy}" r="14" fill="{fill}"/><text x="{cx:.1f}" y="{cy+5.5}" font-size="15" font-weight="800" fill="#fff" text-anchor="middle">{i+1}</text>')
        lines = txt.split("|")
        for j, ln in enumerate(lines):
            a(f'<text x="{cx:.1f}" y="{cy+48+j*23}" font-size="17" font-weight="{700 if j == 0 else 500}" fill="{INK if not before else "#5f5146"}" text-anchor="middle">{ln}</text>')

for r in ROWS:
    y = r["y"]
    a(f'<text x="48" y="{y-6}" font-size="14" font-weight="800" fill="{OR}" letter-spacing="2">{r["no"]}</text>')
    a(f'<text x="80" y="{y-6}" font-size="23" font-weight="800" fill="{INK}">{r["title"]}</text>')
    lane(y + 6, r["before"], r["before_actor"], True)
    lane(y + 6 + LANE_H + LANE_GAP, r["after"], r["after_actor"], False)
    # 통계 카드 (두 레인에 걸침)
    top = y + 6; hh = LANE_H * 2 + LANE_GAP
    a(f'<rect x="{SX}" y="{top}" width="232" height="{hh}" rx="22" fill="#fff" stroke="#f2e4d6" stroke-width="1.5" filter="url(#sh2)"/>')
    b, af, cap, sub = r["stat"]
    cx = SX + 116
    a(f'<text x="{cx}" y="{top+76}" font-size="15" fill="{MUTED}" text-anchor="middle" font-weight="600">{cap}</text>')
    a(f'<text x="{cx-52}" y="{top+156}" font-size="78" font-weight="800" fill="#cfc3b8" text-anchor="middle">{b}</text>')
    a(f'<path d="M{cx-18} {top+128} h26 m-9 -9 l9 9 l-9 9" fill="none" stroke="{OR}" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>')
    a(f'<text x="{cx+60}" y="{top+156}" font-size="78" font-weight="800" fill="{OR}" text-anchor="middle">{af}</text>')
    a(f'<text x="{cx}" y="{top+196}" font-size="14.5" fill="{MUTED}" text-anchor="middle">{sub}</text>')
# 하단 원칙
a(f'<rect x="48" y="806" width="1504" height="50" rx="25" fill="{INK}"/>')
a(f'<text x="800" y="837" font-size="19" font-weight="700" fill="#fff" text-anchor="middle">반복 작업은 <tspan fill="{OR}">AI</tspan>가, 판단(검토·승인)은 <tspan fill="{OR}">사람</tspan>이 — 모르면 추측하지 않고 사람에게 넘깁니다</text>')
a('</svg>')
Path(__file__).with_name("workflow.svg").write_text("\n".join(out), encoding="utf-8")
print("saved")
