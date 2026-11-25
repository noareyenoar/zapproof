# Telegram daily brief and outage alerts

**สถานะ:** พร้อมใช้ — รอเจ้าของสร้าง bot และใส่ secrets (2026-11-25)

เจ้าของอาจไม่ได้เปิดคอมพิวเตอร์ทุกวัน ระบบนี้จึงรันบน **GitHub Actions** ไม่ใช่บนเครื่อง
เจ้าของและไม่ใช่ใน Claude Code: สรุปสถานะโปรเจกต์วันละครั้ง และแจ้งเตือนทันทีเมื่อ
surface สาธารณะล้ม โดยส่งเข้า Telegram

**ค่าใช้จ่าย:** $0 — GitHub Actions ใช้ free minutes 2,000 นาที/เดือนของ private repo
(ระบบนี้ใช้ประมาณ 250–300 นาที/เดือน), Telegram Bot API ฟรี, และ Gemini API ใช้ free tier
(ถ้า quota หมด จะส่งสรุปแบบ template แทน ไม่เงียบ)

## สิ่งที่ระบบทำ

| โหมด | เวลา | เนื้อหา |
|---|---|---|
| `daily` | ทุกวัน 03:30 UTC = **10:30 เวลาไทย** | คอมมิต 24 ชม., สถานะ 4 surface, ตัวเลขผู้เรียกภายนอก 30 วัน, สรุปภาษาไทยโดย LLM (ไม่เกิน ~700 ตัวอักษร) |
| `health` | ทุกชั่วโมงที่ :17 | ตรวจ 4 surface; ล้ม → ส่ง Telegram ทันที; ล้มเหมือนเดิม → เตือนซ้ำทุก 6 ชม.; หาย → ส่งข้อความ "กลับมาปกติ" |

Surface ที่ตรวจ:

1. `GET /v1/health` — ต้อง 200, `sellers_indexed` และ `as_of_block` ต้องเป็นจำนวนเต็มบวก
   (endpoint นี้เปิดฐานข้อมูลจริง จึงพิสูจน์ว่า index ยังใช้งานได้)
2. `POST /mcp` initialize — ต้อง 200
3. `GET https://zapproof.web.app/` — ต้อง 200 และมีคำว่า Zapproof
4. `GET https://zapproof.web.app/agents/` — ต้อง 200

**ไม่ probe `/v1/score`** โดยตั้งใจ: metrics นับ score call เป็น external demand และ
ตัวเฝ้าระวังต้องไม่ทำให้ตัวเลขที่ตัวเองรายงานสูงขึ้น การตรวจ `/v1/health` ครอบคลุม
การเปิดฐานข้อมูลและ index ที่ไม่ว่างอยู่แล้ว

ไฟล์ที่เกี่ยวข้อง: `scripts/telegram_brief.py`,
`.github/workflows/daily-brief.yml`, `.github/workflows/health-watch.yml`,
tests ที่ `tests/test_telegram_brief.py`

## ตั้งค่าครั้งเดียว (~10 นาที) — เจ้าของทำ

> **ห้ามใส่ token หรือ API key ลงในไฟล์ใน repo ทุกกรณี** (รวม `.env` ที่ push ขึ้นไป)
> ค่าเหล่านี้ใส่ได้เฉพาะ GitHub **Secrets** เท่านั้น — `.env` อยู่แต่ในเครื่องและถูก
> gitignore ไว้; ถ้าหลุดเข้า git history เมื่อไร ต้อง revoke ทันทีเพราะลบยาก

### 1. สร้าง Telegram bot

1. เปิด Telegram → ค้นหา **@BotFather** → `/newbot` → ตั้งชื่อและ username
2. BotFather จะให้ token (หน้าตา `123456789:AA…`) — **ห้ามส่ง token ในแชตหรือภาพหน้าจอ**
3. เปิดแชตกับ bot ที่สร้าง → กด **Start** → ส่งข้อความอะไรก็ได้หนึ่งครั้ง
4. เปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` ในเบราว์เซอร์ แล้วหา
   `"chat":{"id":…}` — ค่านั้นคือ `TELEGRAM_CHAT_ID`
   (ถ้าใช้กลุ่ม: เพิ่ม bot เข้ากลุ่ม ส่งข้อความในกลุ่ม แล้วใช้ id ที่เป็นเลขลบ)

### 2. สร้าง Gemini API key (ทางที่แนะนำ — ฟรี)

1. ไปที่ <https://aistudio.google.com/apikey> → **Create API key**
2. คัดลอก key เก็บไว้ — จะใส่ใน GitHub secrets เท่านั้น
3. หมายเหตุ free tier: Google อาจใช้ข้อมูลที่ส่งเข้าโมเดลเพื่อปรับปรุงผลิตภัณฑ์
   สรุปของเราส่งเฉพาะข้อมูลที่เป็น public อยู่แล้ว (ข้อความคอมมิต, สถานะ endpoint,
   ตัวเลข usage สาธารณะ) — **ไม่มี secret และไม่มี chat id**

### 3. ใส่ secrets ใน GitHub

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| ชื่อ | ค่า |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token จาก BotFather |
| `TELEGRAM_CHAT_ID` | id จากขั้นที่ 1 |
| `GEMINI_API_KEY` | key จากขั้นที่ 2 (ถ้าไม่ใส่ ระบบยังส่งสรุปแบบ template) |

ถ้าต้องการเปลี่ยนรุ่นโมเดลโดยไม่แก้โค้ด: สร้าง **Repository variable** ชื่อ
`GEMINI_MODEL` (ค่าเริ่มต้น `gemini-2.5-flash`)

### 4. เปิดใช้และทดสอบ

1. ตรวจว่า workflow อยู่บน **default branch** ของ repo (schedule จะรันเฉพาะ branch นั้น)
2. แท็บ **Actions** → **Daily brief to Telegram** → **Run workflow**
3. ตรวจว่ามีข้อความเข้า Telegram; ทำซ้ำกับ **Health watch (Telegram)**
4. ถ้าอยากทดสอบเฉพาะ health alert โดยไม่รอให้ของจริงล้ม: รัน
   `uv run python scripts/telegram_brief.py health --dry-run` บนเครื่อง (ไม่ส่ง)

## ใช้ Google Enterprise (Vertex AI / Agent Builder) ได้ไหม

จากภาพ: `vertexaisearch.cloud.google.com` → **Agent garden / Gemini Enterprise**
คือ Vertex AI Agent Builder — เครื่องมือสร้าง **chat agent องค์กร** ที่ต่อ data store
ไม่ใช่ scheduler และไม่มีกลไกส่งข้อความตามเวลา อธิบายตรง ๆ:

- **ตัว Agent Builder ใช้แทนระบบนี้ไม่ได้** — ถึงสร้าง agent ได้ ก็ยังต้องมี scheduler
  และ Telegram bot เหมือนเดิม
- **ใช้โมเดล Gemini ตัวเดียวกันผ่าน Vertex AI ได้** — แต่ต้องเปิด billing บน GCP project,
  เปิด Vertex AI API และใช้ service account; และ **Gemini Enterprise เป็นผลิตภัณฑ์แบบ
  ขายเป็น seat ต่อผู้ใช้/เดือน** ซึ่งไม่เข้ากับเพดาน $0 ของโปรเจกต์
- ถ้า **Google Enterprise AI Pass** ที่เห็นในประวัติแชตมีสิทธิ์ Vertex AI ให้ใช้
  ต้องตรวจเงื่อนไขของ pass ก่อน (อาจมีวันหมดอายุ/จำกัดรุ่นโมเดล)
- **ทางที่แนะนำสำหรับงานนี้:** Gemini API key จาก AI Studio (free tier) — โมเดลตระกูล
  เดียวกัน, HTTP call เดียว, ไม่ต้องมี billing, ไม่ต้องมี seat

ถ้าภายหลังต้องการย้ายไป Vertex จริง: แก้ฟังก์ชัน `llm_summary()` ใน
`scripts/telegram_brief.py` ให้เรียก endpoint ของ Vertex แทน Generative Language API
(ใช้ OAuth access token ของ service account) — ที่เหลือทั้งระบบไม่เปลี่ยน เพราะสรุปเป็น
ข้อความแล้วส่งเข้า Telegram เหมือนกัน

## สิ่งที่ระบบนี้ *ไม่* ครอบคลุม

- อันดับ Glama, ความสดของข้อมูล ingestion, การเปลี่ยนแปลงของ settlement, หรือ
  security incident ภายใน GCP — เพิ่มได้โดยแก้ `run_checks()` ในสคริปต์
- GitHub Actions อาจรันช้ากว่าเวลาที่ตั้ง (ปกติไม่กี่นาที) — ไม่ใช่ real-time pager
- **repo งานนี้เป็น private** — GitHub อาจปิด scheduled workflow อัตโนมัติถ้า repo ไม่มี
  activity 60 วันติด ถ้าโปรเจกต์หยุด commit นาน ต้องเข้า Actions แล้วกด re-enable
  (public repo `noareyenoar/zapproof` เป็นคนละ repo และไม่มี workflow นี้)
- workflow ต้องอยู่บน default branch (`docs/ln-phase0-2026-11-16`) — push แล้ว 2026-11-25

## ทดสอบในเครื่อง

```bash
uv run python scripts/telegram_brief.py daily --dry-run
uv run python scripts/telegram_brief.py health --dry-run
```

ถ้าจะส่งจริงจากเครื่อง: ตั้ง `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` และ
(ถ้าต้องการ LLM) `GEMINI_API_KEY` ใน environment แล้วรันโดยไม่ใส่ `--dry-run` —
ห้าม commit ค่าเหล่านี้

## หมุนเวียน/เพิกถอน

- Bot token รั่ว: BotFather → `/revoke` → อัปเดต secret
- Gemini key: ลบใน AI Studio แล้วสร้างใหม่ → อัปเดต secret
- ข้อความทุกข้อความถูก redact ค่า secret ก่อนพิมพ์หรือส่ง และ HTTP error รายงาน
  เฉพาะ status code ไม่รายงาน URL ที่มี token
