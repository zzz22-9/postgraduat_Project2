-- 仅清理 2026-11-04 用户明确指定的四台测试设备。
-- 先备份，在开启 foreign_keys 的事务内执行；运行中设备不得清理。
-- equipment_id 是真实主键，name 是用户在网页上看到的名称。
CREATE TEMP TABLE cleanup_targets (equipment_id TEXT PRIMARY KEY, name TEXT);
INSERT INTO cleanup_targets VALUES
    ('EQ-8a51b8f87947475183cae26e535026bb', 'E-ASM-2'),
    ('EQ-ad3067b230f74f6aa5945da352c688de', 'E-MOVE-2'),
    ('EQ-abb8ea47c10f4f24b7739815640c44c6', 'E-QC-2'),
    ('EQ-f295fe048e3647d5b31cdb76315a1a3b', 'E-MOVE-1');

-- 防止误用：主键与名称须匹配，相关工序必须已完成，不能有返修任务引用。
-- CHECK 失败时由调用者回滚整个事务。
CREATE TEMP TABLE cleanup_guard (ok INTEGER CHECK (ok = 1));
INSERT INTO cleanup_guard SELECT CASE WHEN
    NOT EXISTS (SELECT 1 FROM equipment e JOIN cleanup_targets t USING(equipment_id)
                WHERE e.name != t.name)
    AND NOT EXISTS (SELECT 1 FROM operations WHERE equipment_id IN
                    (SELECT equipment_id FROM cleanup_targets) AND status != 'completed')
    AND NOT EXISTS (SELECT 1 FROM reworks WHERE equipment_id IN
                    (SELECT equipment_id FROM cleanup_targets))
    THEN 1 ELSE 0 END;

-- 保留事件原文，并将旧设备身份转为文字快照；不把历史生产改记到核心设备。
UPDATE events
SET detail = detail || ' [设备清理归档 2026-11-04：' ||
             (SELECT name FROM cleanup_targets t WHERE t.equipment_id = events.equipment_id) ||
             ' / ' || equipment_id || ']',
    equipment_id = NULL
WHERE equipment_id IN (SELECT equipment_id FROM cleanup_targets);

-- 5 道已完成工序解除设备外键，数量、状态和报工不变；旧身份可查事件和备份。
UPDATE operations SET equipment_id = NULL
WHERE equipment_id IN (SELECT equipment_id FROM cleanup_targets);

-- 返修动作中的设备编号虽不是外键，也同步归档到说明，避免残留失效引用。
UPDATE rework_actions
SET note = note || ' [设备清理归档 2026-11-04：' ||
           (SELECT name FROM cleanup_targets t WHERE t.equipment_id = rework_actions.equipment_id) ||
           ' / ' || equipment_id || ']',
    equipment_id = NULL
WHERE equipment_id IN (SELECT equipment_id FROM cleanup_targets);

DELETE FROM equipment
WHERE equipment_id IN (SELECT equipment_id FROM cleanup_targets);

UPDATE equipment SET name = 'CNC_01 (组装/加工工位 E-ASM)' WHERE equipment_id = 'E-ASM';
UPDATE equipment SET name = 'Vision_01 (视觉质检站 E-QC)' WHERE equipment_id = 'E-QC';
UPDATE equipment SET name = 'Robot_01 (搬运机械臂 E-MOVE)' WHERE equipment_id = 'E-MOVE';

DROP TABLE cleanup_guard;
DROP TABLE cleanup_targets;
