-- ============================================================
-- 003_optional_otp.sql
-- รองรับโหมด "ล็อกอินด้วยอีเมลอย่างเดียว" (ปิด OTP)
--
-- เมื่อปิด OTP อีเมลกลายเป็นหลักฐานชิ้นเดียว ด่านที่เหลือจึงมีแค่
--   (1) จำนวนเครื่องที่ผูกได้
--   (2) การจำกัดอัตราการพยายามผูกเครื่อง
-- ข้อ (2) ต้องนับต่อ IP เพราะเป็นค่าเดียวที่ผู้ยิงปลอมไม่ได้ (hwid ฝั่งเกม
-- ส่งมาเอง) แต่ auth_log ยังไม่มีคอลัมน์เก็บ IP
-- ============================================================

alter table auth_log add column if not exists request_ip text;

comment on column auth_log.request_ip is
    'IP ของผู้เรียก อ่านจากรายการสุดท้ายใน X-Forwarded-For (ตัวที่ Render เติม)';

-- ใช้นับจำนวนครั้งที่พยายามผูกเครื่องจาก IP หนึ่ง ๆ ในช่วงเวลา
create index if not exists auth_log_ip_window_idx
    on auth_log (request_ip, created_at desc);

-- ใช้กรองเฉพาะเหตุการณ์ที่สนใจตอนนับ (activate_ok / activate_denied)
create index if not exists auth_log_event_window_idx
    on auth_log (event, created_at desc);
