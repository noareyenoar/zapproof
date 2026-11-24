# Zapproof (ภาษาไทย)

ดัชนีหลักฐานการชำระเงินแบบอ่านอย่างเดียวและไม่ถือเงินของใคร สำหรับ AI agent
สร้างจากหลักฐานการชำระเงินที่ settled แล้วบน Base

**สถานะ:** hosted API และ remote MCP ใช้งานได้; แพ็กเกจ `zapproof-mcp` อยู่บน PyPI;
official MCP Registry เปิดแล้ว (`io.github.noareyenoar/x402-counterparty-reputation`);
Glama connector เปิดแล้ว; Cursor จะ submit หลัง public repo นี้พร้อม การตีความใช้กฎและ
**ยังไม่ผ่านการตรวจสอบอย่างอิสระ** — ทุกคำตอบระบุข้อจำกัดนี้

- หน้าเว็บ: <https://zapproof.web.app>
- MCP endpoint: `https://zapproof-hc75zyauja-uc.a.run.app/mcp`
- PyPI: <https://pypi.org/project/zapproof-mcp/>

## มันตอบอะไร

Zapproof ตอบคำถามเดียวสำหรับ agent ที่กำลังจะจ่ายเงินให้คนแปลกหน้า:
**มีหลักฐานว่าคู่ค้ารายนี้ทำธุรกิจจริงหรือไม่** — และหลักฐานนั้นบอกอะไรได้/ไม่ได้

คะแนนสร้างจากหลักฐานการชำระเงินที่ settled แล้วเท่านั้น ไม่มีระบบให้ดาว
ไม่มีคะแนนจากผู้ใช้ และไม่มีทางซื้อสถานะ

## สิ่งที่มันไม่ทำ

- ไม่ถือเงิน ไม่ escrow และไม่ควบคุมเงินของใคร
- ไม่เป็นจุดบังคับในเส้นทางจ่ายเงิน; ถ้า Zapproof ล่ม การจ่ายเงินของบุคคลที่สาม
  ต้องไม่ถูกบล็อกหรือหน่วง
- ไม่ตัดสินความซื่อสัตย์ของบุคคลหรือบริษัท และไม่แปลง "ไม่พบข้อมูล" เป็นคะแนนต่ำ
- ไม่ให้คะแนน Lightning; rail นั้นยังไม่ถูก index (`rail_not_indexed`)

## เริ่มใช้

### Hosted MCP

```json
{
  "mcpServers": {
    "zapproof": {
      "type": "http",
      "url": "https://zapproof-hc75zyauja-uc.a.run.app/mcp"
    }
  }
}
```

มีเครื่องมือเดียว: `check_counterparty` — ไม่มีสิทธิ์เขียน ไม่มี wallet และไม่ต้องมี key

### Agent plugin (Cursor และไคลเอนต์ที่รองรับ Agent Plugins)

repo นี้ติดตั้งเป็น plugin ได้เอง:

- `.mcp.json` และ `.cursor-plugin/plugin.json` ลงทะเบียน hosted MCP endpoint สำหรับ Cursor
- `skills/zapproof/SKILL.md` บอก agent ว่าควรตรวจคู่ค้าเมื่อใดและอ่านคำตอบอย่างไร
- `skills/x402-payer/SKILL.md` เป็นคู่มือฟรีสำหรับจ่าย x402 จากกระเป๋า Base โดยเฉพาะ
  พร้อมโค้ดอ้างอิงที่ `scripts/preflight_payer.py`
- `rules/zapproof.mdc` เป็น Cursor rule เสริมสำหรับการตรวจก่อนจ่าย
- `plugin.json` และ `mcp.json` ที่ root ใช้รูปแบบ Agent Plugins 1.0

### Offline package

```sh
uvx zapproof-mcp
```

wheel บน PyPI แนบ snapshot ของ seller 19,397 ราย (Base block 53,201,719,
methodology v1) จึงใช้ได้โดยไม่ต้องมี key, wallet หรือเครือข่าย
address ที่ไม่พบใน snapshot จะได้ `found: false` และ `status: "unknown"` — ไม่ใช่ 0

### จาก source

```sh
git clone https://github.com/noareyenoar/zapproof.git
cd zapproof
uv sync --group dev
```

repo ไม่มี ingest database หรือ snapshot (ข้อมูลที่สร้างใหม่ได้) — ใช้
`ZAPPROOF_API_URL` ชี้ไปที่ hosted index หรือสร้าง snapshot จากฐานข้อมูลท้องถิ่น
ตาม `scripts/build_snapshot.py` และ `docs/METHODOLOGY.md`

## อ่านข้อจำกัดคู่กับตัวเลข

การตีความเป็นแบบกฎ และยังไม่ผ่านการตรวจสอบอย่างอิสระ การตัดสินแบบ blinded
เห็นด้วยประมาณ 62% ของครั้ง (83% เมื่อไม่นับ abstentions) และผู้ตัดสินเป็น
language model ไม่ใช่มนุษย์

- `found: false` และ `insufficient_evidence` ไม่ใช่สัญญาณลบ
- คะแนน null ไม่ใช่ศูนย์
- Lightning ยังไม่ถูก index
- `signaling_likelihood` สูงเป็นข้อความเกี่ยวกับรูปแบบการชำระเงิน ไม่ใช่คำตัดสินเกี่ยวกับ entity

## การพัฒนา

```sh
uv run pytest -q
uv run ruff check .
uv run mypy
uv run python gates/gate_B.py
```

## หลักฐาน

ตัวเลขทุกตัวที่เผยแพร่ต้องผลิตด้วย script จากฐานข้อมูล พร้อมคำสั่ง reproduce
ผล gate อยู่ใน `reports/`:

- `reports/gate_A.json` — การตรวจ methodology และความถูกต้อง รวมข้อที่ไม่ผ่าน
- `reports/gate_B.json` — การตรวจ hosted surface
- `reports/crosscheck_A.json` — เทียบจำนวนและยอดกับ log จาก Base RPC ผู้ให้บริการอิสระ
  ใน 3 วัน (ตรวจการ decode/count ของเรา ไม่ใช่ independent indexer)
- `reports/planted_pattern_results.json` — สิ่งที่ classifier จับได้ และข้อจำกัด
  เชิงวงกลมของ test

## สัญญาอนุญาต

MIT ข้อมูลคะแนนไม่อยู่ภายใต้สัญญาอนุญาตนี้

English: [README.md](README.md)
