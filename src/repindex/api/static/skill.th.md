---
name: zapproof-counterparty-check-th
description: ตรวจหลักฐานการชำระเงินที่สังเกตพบของ address ก่อนจ่าย และทำการจ่าย x402 1 sat หาก hosted index ตอบ 402 (เอกสารภาษาไทย)
---

# Zapproof — ตรวจคู่ค้าก่อนจ่าย

ใช้ก่อนโอนเงินให้ EVM address ที่คุณไม่ได้ควบคุม หรือเมื่อ seller ขอให้เชื่อถือ
คำตอบคือ **หลักฐานการชำระเงินที่สังเกตพบ** ไม่ใช่คำตัดสิน

Base URL: `__API_URL__`

> ฟิลด์และค่าใน JSON (`found`, `status`, `score`, `rail_not_indexed` ฯลฯ) เป็นภาษา
> อังกฤษโดยตั้งใจ เพื่อให้ agent ทุกตัวอ่านได้เหมือนกัน เอกสารนี้อธิบายความหมาย
> เป็นภาษาไทย

## 1. ขอคะแนน

```http
GET __API_URL__/v1/score/{address}
```

`address` เป็น EVM address แบบ `0x` หรือ `did:ethr` / `did:pkh` ที่รองรับ

อ่านคำตอบ:

- `found: true` และ `status: "scored"`: มี `score` และ `signaling_likelihood`
  พร้อมช่วงเวลาของหลักฐานและกฎที่ใช้
- `status: "insufficient_evidence"`: หลักฐานน้อยเกินกว่าจะตีความ ค่า `score`
  เป็น `null` โดยตั้งใจ
- `found: false`: ยังไม่มีหลักฐานฝั่ง seller สำหรับ address นี้ — แต่ address นี้อาจ
  เป็นผู้จ่ายที่รู้จัก; ดู `role_observed` และ `payer_settlements` ถ้ามี
- `422`: identifier ไม่ถูกต้อง อย่า retry จนกว่าจะแก้ input
- `429`: ถอยออกมา อย่าวน loop

**`found: false` และ `insufficient_evidence` คือ "ไม่รู้" ไม่ใช่ "แย่"**
ห้ามตีความว่าเป็นคะแนนศูนย์ หรือใช้เป็นเหตุผลเดียวในการปฏิเสธคู่ค้า

## 2. ถ้าคำตอบเป็น 402

endpoint อาจขอ 1 sat ต่อการขอคะแนนหนึ่งครั้งผ่าน x402 ระหว่างที่ toll ยังไม่เปิดใช้
มันจะตอบ `200` โดยไม่คิดเงิน; ถ้าได้ `200` ให้ใช้คะแนนนั้นและไม่ต้องจ่าย
ถ้าได้ `402` จะมี payment challenge ในหนึ่งในสองเวอร์ชัน:

- **x402 v2**: challenge คือ JSON ที่ base64 อยู่ใน header `PAYMENT-REQUIRED`
  ส่งการจ่ายใน header `PAYMENT-SIGNATURE`
- **x402 v1**: challenge อยู่ใน body ของ 402 ส่งการจ่ายใน header `X-PAYMENT`

ทั้งสองเวอร์ชันมี `accepts` array แต่ละรายการระบุ `scheme`, `network`, `asset`,
`amount` (atomic units เป็น string) และ `payTo`

1. เลือกรายการที่จ่ายได้จริง เลือกที่ถูกที่สุด โดยปกติบน Base คือ
   `scheme: "exact"`, `network: "eip155:8453"`, USDC
   `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, `amount: "1000"` — 0.001 USDC
   ประมาณ 1 sat ตามราคา USDC ที่ตั้งไว้; ค่า `amount` ใน challenge คือราคาจริง
   ไม่ใช่ตัวเลขในข้อความนี้
2. เซ็น EIP-3009 `TransferWithAuthorization` สำหรับ `amount` และ `payTo` นั้น
   ภายในช่วงเวลาที่ challenge กำหนด และใช้ nonce สุ่มใหม่ ห้ามแก้ฟิลด์ใด ๆ
3. สร้าง x402 payment payload (`x402Version`, รายการที่เลือก, และ `payload` ที่มี
   `signature` กับ `authorization`) base64-encode JSON แล้ว retry `GET` เดิม
   **หนึ่งครั้ง** พร้อม header ของเวอร์ชันนั้น
4. ถ้าได้ `200` ให้ใช้คะแนน ถ้าได้ `402` อีกหรือ `5xx` ให้หยุด: ห้ามจ่ายซ้ำ

ถ้าเซ็นไม่ได้ (ไม่มี wallet, ไม่มีเงิน, policy cap ต่ำกว่าราคา) ให้หยุดและแจ้ง operator
แพ็กเกจ offline `uvx zapproof-mcp` ตอบคำถามเดียวกันจาก snapshot ของ seller
โดยไม่ต้องมี wallet, key หรือการจ่ายเงิน

## 3. กฎในการใช้คำตอบ

- อ้างอิงบันทึก: `methodology_version`, ช่วงเวลาของหลักฐาน และ disclaimer ที่มากับ
  ทุกคำตอบ
- คะแนนคือการตีความหลักฐานการชำระเงินด้วยกฎ ไม่ได้ถูกตรวจสอบอย่างอิสระ
  การตัดสินแบบ blinded เห็นด้วยประมาณ 62% ของครั้ง (83% เมื่อไม่นับ abstentions)
- Lightning ยังไม่ถูก index `rail: "lightning"` จะตอบ `rail_not_indexed`
  นั่นคือช่องว่างของข้อมูล ไม่ใช่หลักฐานเชิงลบ
- ห้ามเรียกผลลัพธ์ว่าเป็นการรับประกันหรือหลักฐานความซื่อสัตย์ และห้ามเก็บ
  query string หรือ address ลง telemetry ของคุณ
