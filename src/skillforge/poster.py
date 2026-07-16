"""Generate a one-page A4 training poster from a reviewed SkillForge SOP."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from .contracts import validate_document


OUTPUT = Path("output/pdf/n31_a4_training_poster.pdf")
FONT_REGULAR = "SkillForgePosterRegular"
FONT_BOLD = "SkillForgePosterBold"
FONT_PAIRS = (
    (
        Path.home() / "Library/Fonts/Heiti SC Medium.ttf",
        Path.home() / "Library/Fonts/Heiti SC Medium.ttf",
    ),
    (
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ),
    (
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttf"),
    ),
)

INK = HexColor("#12211A")
MUTED = HexColor("#53655B")
PAPER = HexColor("#F5F8F3")
LINE = HexColor("#D9E5DC")
GREEN = HexColor("#22C56E")
GREEN_DARK = HexColor("#087A43")
PHASE_COLORS = {
    "准备": HexColor("#1C9B61"),
    "设备": HexColor("#2779BD"),
    "校准": HexColor("#7457C7"),
    "验收": HexColor("#D48324"),
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def register_fonts() -> tuple[str, str]:
    for regular, bold in FONT_PAIRS:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(regular)))
            pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold)))
            return FONT_REGULAR, FONT_BOLD
    fallback = "STSong-Light"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    return fallback, fallback


def _phase(step_id: str) -> str:
    number = int(step_id[1:])
    if number <= 3:
        return "准备"
    if number <= 7:
        return "设备"
    if number <= 11:
        return "校准"
    return "验收"


def _parameter(sop: dict[str, Any], name: str, default: str) -> str:
    for step in sop["steps"]:
        for item in step["parameters"]:
            if item["name"] == name:
                suffix = item["unit"]
                return f"{item['value']}{suffix}"
    return default


def _paragraph(
    text: str,
    *,
    font: str,
    size: float,
    leading: float,
    color: Color = INK,
    alignment: int = 0,
) -> Paragraph:
    return Paragraph(
        html.escape(text),
        ParagraphStyle(
            name=f"poster-{font}-{size}-{leading}-{alignment}",
            fontName=font,
            fontSize=size,
            leading=leading,
            textColor=color,
            alignment=alignment,
            wordWrap="CJK",
            splitLongWords=True,
            spaceAfter=0,
            spaceBefore=0,
        ),
    )


def _draw_paragraph(
    c: canvas.Canvas,
    paragraph: Paragraph,
    x: float,
    y_top: float,
    width: float,
    max_height: float,
) -> float:
    _, height = paragraph.wrap(width, max_height)
    paragraph.drawOn(c, x, y_top - height)
    return height


def _draw_fact(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    label: str,
    value: str,
    regular: str,
    bold: str,
) -> None:
    c.setFillColor(white)
    c.roundRect(x, y, width, 31, 7, fill=1, stroke=0)
    c.setFillColor(MUTED)
    c.setFont(regular, 6.6)
    c.drawString(x + 9, y + 18.5, label)
    c.setFillColor(INK)
    c.setFont(bold, 9.2)
    c.drawString(x + 9, y + 7.5, value)


def _fit_action(
    text: str,
    font: str,
    width: float,
    max_height: float,
) -> Paragraph:
    for size in (7.2, 6.9, 6.6, 6.3, 6.0):
        paragraph = _paragraph(
            text,
            font=font,
            size=size,
            leading=size + 2.0,
            color=MUTED,
        )
        _, height = paragraph.wrap(width, max_height)
        if height <= max_height:
            return paragraph
    return paragraph


def _draw_step(
    c: canvas.Canvas,
    step: dict[str, Any],
    x: float,
    y: float,
    width: float,
    height: float,
    regular: str,
    bold: str,
) -> None:
    phase = _phase(step["step_id"])
    color = PHASE_COLORS[phase]
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.roundRect(x, y, width, height, 7, fill=1, stroke=1)
    c.setFillColor(color)
    c.roundRect(x, y + height - 4, width, 4, 2, fill=1, stroke=0)

    c.setFillColor(color)
    c.roundRect(x + 8, y + height - 25, 31, 15, 6, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 7.3)
    c.drawCentredString(x + 23.5, y + height - 20.7, step["step_id"])

    conditional = not step["required"]
    title_width = width - 55 - (30 if conditional else 0)
    title = _paragraph(
        step["title"],
        font=bold,
        size=8.4,
        leading=9.7,
        color=INK,
    )
    _draw_paragraph(
        c,
        title,
        x + 45,
        y + height - 10,
        title_width,
        21,
    )
    if conditional:
        c.setFillColor(HexColor("#EEF2EF"))
        c.roundRect(x + width - 31, y + height - 24, 23, 14, 5, fill=1, stroke=0)
        c.setFillColor(MUTED)
        c.setFont(regular, 6.3)
        c.drawCentredString(x + width - 19.5, y + height - 20.1, "条件")

    action_height = height - 34
    action = _fit_action(step["action"], regular, width - 18, action_height)
    _draw_paragraph(
        c,
        action,
        x + 9,
        y + action_height + 4,
        width - 18,
        action_height,
    )


def _draw_troubleshooting(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    regular: str,
    bold: str,
) -> None:
    c.setFillColor(HexColor("#FFF6E7"))
    c.setStrokeColor(HexColor("#E9C27E"))
    c.roundRect(x, y, width, height, 7, fill=1, stroke=1)
    c.setFillColor(HexColor("#9A5B09"))
    c.setFont(bold, 8.4)
    c.drawString(x + 10, y + height - 18, "异常时先停止，不要强拉")
    text = (
        "连续跳纸或停位错误：重新核对缝标模式、导轨松紧和介质学习。"
        "出现报警、异味、异常声音或卡纸：断电后检查纸路。"
    )
    paragraph = _paragraph(
        text,
        font=regular,
        size=6.8,
        leading=8.7,
        color=HexColor("#77501B"),
    )
    _draw_paragraph(c, paragraph, x + 10, y + height - 25, width - 20, height - 30)


def generate_poster(
    sop_path: Path,
    destination: Path = OUTPUT,
) -> Path:
    sop = _read_json(sop_path)
    validate_document(sop, "sop.schema.json")
    if not 8 <= len(sop["steps"]) <= 15:
        raise ValueError("A4海报仅支持8到15步SOP")
    destination.parent.mkdir(parents=True, exist_ok=True)
    regular, bold = register_fonts()

    page_width, page_height = A4
    c = canvas.Canvas(
        str(destination),
        pagesize=A4,
        invariant=1,
        pageCompression=1,
    )
    c.setTitle(f"{sop['title']} - A4培训海报")
    c.setAuthor("SkillForge 匠传")
    c.setSubject("Evidence-grounded N31 training poster")

    c.setFillColor(PAPER)
    c.rect(0, 0, page_width, page_height, fill=1, stroke=0)
    c.setFillColor(INK)
    c.rect(0, page_height - 104, page_width, 104, fill=1, stroke=0)
    c.setFillColor(GREEN)
    c.roundRect(28, page_height - 31, 132, 17, 8, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont(bold, 7.3)
    c.drawCentredString(94, page_height - 26.3, "SkillForge 匠传 · Gold v1")

    title = _paragraph(
        sop["title"],
        font=bold,
        size=19.5,
        leading=23,
        color=white,
    )
    _draw_paragraph(c, title, 28, page_height - 42, page_width - 56, 52)
    c.setFillColor(HexColor("#B9CCC0"))
    c.setFont(regular, 7.3)
    c.drawRightString(
        page_width - 28,
        page_height - 92,
        "操作前核对设备与介质；异常时停止并进入检查流程",
    )

    fact_y = page_height - 140
    fact_gap = 7
    fact_width = (page_width - 56 - fact_gap * 3) / 4
    facts = (
        ("设备", "汉印 N31"),
        (
            "介质",
            f"{_parameter(sop, '本批标签宽度', '72mm')} × "
            f"{_parameter(sop, '本批标签高度', '130mm')}",
        ),
        ("定位", _parameter(sop, "定位方式", "缝标")),
        ("Gold步骤", f"{len(sop['steps'])} 步"),
    )
    for index, (label, value) in enumerate(facts):
        _draw_fact(
            c,
            28 + index * (fact_width + fact_gap),
            fact_y,
            fact_width,
            label,
            value,
            regular,
            bold,
        )

    column_gap = 12
    column_width = (page_width - 56 - column_gap) / 2
    body_top = fact_y - 12
    body_bottom = 137
    row_gap = 6
    row_height = (body_top - body_bottom - row_gap * 6) / 7
    left = sop["steps"][:7]
    right = sop["steps"][7:]
    for index, step in enumerate(left):
        y = body_top - (index + 1) * row_height - index * row_gap
        _draw_step(c, step, 28, y, column_width, row_height, regular, bold)
    for index, step in enumerate(right[:6]):
        y = body_top - (index + 1) * row_height - index * row_gap
        _draw_step(
            c,
            step,
            28 + column_width + column_gap,
            y,
            column_width,
            row_height,
            regular,
            bold,
        )
    final_y = body_top - 7 * row_height - 6 * row_gap
    _draw_troubleshooting(
        c,
        28 + column_width + column_gap,
        final_y,
        column_width,
        row_height,
        regular,
        bold,
    )

    footer_y = 30
    footer_height = 92
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.roundRect(28, footer_y, page_width - 56, footer_height, 9, fill=1, stroke=1)
    c.setFillColor(GREEN_DARK)
    c.setFont(bold, 8.4)
    c.drawString(39, footer_y + 72, "完成标准")
    success = (
        "自动吸纸正常；单张走纸只出一张并停在标签边界附近；"
        "试印完整、基本居中；无连续跳纸、明显偏斜、卡纸或异常报警。"
    )
    _draw_paragraph(
        c,
        _paragraph(success, font=regular, size=7.2, leading=9.3, color=INK),
        39,
        footer_y + 65,
        page_width - 78,
        24,
    )
    c.setFillColor(HexColor("#B75334"))
    c.setFont(bold, 7.5)
    c.drawString(39, footer_y + 34, "隐私提示")
    privacy = (
        "公开演示不得出现姓名、电话、地址、条码、二维码或唯一编号；"
        "打印过程中不要拉扯纸张。"
    )
    _draw_paragraph(
        c,
        _paragraph(
            privacy,
            font=regular,
            size=6.8,
            leading=8.5,
            color=HexColor("#7A4234"),
        ),
        39,
        footer_y + 27,
        page_width - 180,
        20,
    )
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 6.4)
    c.drawRightString(
        page_width - 39,
        footer_y + 14,
        "OPERATOR_REVIEWED_GOLD · evidence details in Gold SOP",
    )

    c.showPage()
    c.save()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sop",
        type=Path,
        default=Path("cases/n31/gold/gold_sop.json"),
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = generate_poster(args.sop, args.output)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
