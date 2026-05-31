INSERT INTO sim_card (iccid, imsi, phone_number, carrier, plan, balance, status, created_at, updated_at)
VALUES ('89860000000000000001', '460000000000001', '+8613301035545', 'china_mobile', 'mvp_test', 0, 'active', now(), now())
ON CONFLICT (iccid) DO NOTHING;

INSERT INTO device_sim_binding (device_id, sim_card_id, is_active, bind_at, created_at, updated_at)
SELECT 3, id, true, now(), now(), now()
FROM sim_card WHERE phone_number = '+8613301035545'
ON CONFLICT DO NOTHING;
