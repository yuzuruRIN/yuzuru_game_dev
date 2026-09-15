-- ============================================================
-- 002_otp_antispam.sql
-- กันการยิง OTP รัวใส่กล่องจดหมายคนอื่น + กันโควตาอีเมลถูกเผา
--
-- ที่มา: rate limit เดิมนับต่ออีเมลอย่างเดียว (3 ครั้ง / 15 นาที) ซึ่งแปลว่า
-- คนที่รู้อีเมลสมาชิกคนหนึ่ง ยิงสคริปต์ส่งเมลหาเหยื่อได้ถึง 288 ฉบับ/วัน
-- และเผาโควตา Resend ฟรี (3,000 ฉบับ/เดือน) หมดภายใน ~10 วัน
--
-- จำกัดต่อ hwid_hash ไม่ช่วย เพราะค่านั้นฝั่งเกมส่งมาเอง สุ่มใหม่ได้ทุกครั้ง
-- ตัวที่ผู้ส่งปลอมไม่ได้คือ IP (Render เติมให้ใน X-Forwarded-For)
-- ============================================================

alter table otp_codes add column if not exists request_ip text;

comment on column otp_codes.request_ip is
    'IP ของผู้ขอรหัส อ่านจากรายการสุดท้ายใน X-Forwarded-For (ตัวที่ Render เติม)';

-- ใช้นับจำนวนคำขอต่อ IP ในช่วงเวลา
create index if not exists otp_codes_ip_window_idx
    on otp_codes (request_ip, created_at desc);

-- ใช้นับจำนวนคำขอต่ออีเมลในช่วงเวลา (ของเดิมมี otp_codes_ratelimit_idx อยู่แล้ว
-- แต่ยืนยันไว้อีกครั้งเผื่อ migration 001 ถูกแก้)
create index if not exists otp_codes_email_window_idx
    on otp_codes (email, created_at desc);
