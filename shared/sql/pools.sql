-- 名额池：同时报名时每个社团一个名额池；分批报名时每个社团每批一个名额池。
-- Python（ensure_schema）与 Rust（ensure_schema_guards）启动时都执行本文件，视图与触发器按这里的定义重建。
-- 执行前 settings 表已有 batch_mode、current_batch 两列。
-- 同时报名时计数只走 registrations(club_id) 索引；分批报名时再按学生所在批次过滤（CASE 只计算选中的分支）。

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS batch_classes (
    class_name TEXT PRIMARY KEY,
    batch_id INTEGER NOT NULL);

-- 学生所在的名额池：同时报名时都为 0；分批报名时为批次编号，班级不在任何批次时为 0。
DROP VIEW IF EXISTS student_batch;
CREATE VIEW student_batch AS
SELECT s.id AS student_id,
       CASE WHEN COALESCE((SELECT batch_mode FROM settings ORDER BY id DESC LIMIT 1), 0) = 0 THEN 0
            ELSE COALESCE(bc.batch_id, 0) END AS pool
FROM students s LEFT JOIN batch_classes bc ON bc.class_name = s.class;

-- 当前名额池的占用：Redis 库存 = max_students - used。分批报名时只计当前批次的学生。
DROP VIEW IF EXISTS club_pool;
CREATE VIEW club_pool AS
SELECT c.id AS club_id, c.max_students AS max_students,
       CASE WHEN COALESCE((SELECT batch_mode FROM settings ORDER BY id DESC LIMIT 1), 0) = 0
            THEN (SELECT COUNT(*) FROM registrations r WHERE r.club_id = c.id)
            ELSE (SELECT COUNT(*) FROM registrations r JOIN student_batch sb ON sb.student_id = r.student_id
                  WHERE r.club_id = c.id
                    AND sb.pool = COALESCE((SELECT current_batch FROM settings ORDER BY id DESC LIMIT 1), 0))
       END AS used
FROM clubs c;

-- 库内最终上限：同一名额池里的报名人数不超过 max_students。
DROP TRIGGER IF EXISTS registrations_capacity_guard;
CREATE TRIGGER registrations_capacity_guard
BEFORE INSERT ON registrations FOR EACH ROW
BEGIN
  SELECT CASE WHEN
    (CASE WHEN COALESCE((SELECT batch_mode FROM settings ORDER BY id DESC LIMIT 1), 0) = 0
          THEN (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id)
          ELSE (SELECT COUNT(*) FROM registrations r JOIN student_batch sb ON sb.student_id = r.student_id
                WHERE r.club_id = NEW.club_id
                  AND sb.pool = COALESCE((SELECT pool FROM student_batch WHERE student_id = NEW.student_id), 0))
     END) >= (SELECT max_students FROM clubs WHERE id = NEW.club_id)
  THEN RAISE(ABORT, 'club full') END;
END;

DROP TRIGGER IF EXISTS registrations_capacity_guard_update;
CREATE TRIGGER registrations_capacity_guard_update
BEFORE UPDATE OF club_id ON registrations
FOR EACH ROW WHEN NEW.club_id != OLD.club_id
BEGIN
  SELECT CASE WHEN
    (CASE WHEN COALESCE((SELECT batch_mode FROM settings ORDER BY id DESC LIMIT 1), 0) = 0
          THEN (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id)
          ELSE (SELECT COUNT(*) FROM registrations r JOIN student_batch sb ON sb.student_id = r.student_id
                WHERE r.club_id = NEW.club_id
                  AND sb.pool = COALESCE((SELECT pool FROM student_batch WHERE student_id = NEW.student_id), 0))
     END) >= (SELECT max_students FROM clubs WHERE id = NEW.club_id)
  THEN RAISE(ABORT, 'club full') END;
END;

-- 人数上限不低于任一名额池里已有的报名人数。
DROP TRIGGER IF EXISTS clubs_capacity_registration_guard;
CREATE TRIGGER clubs_capacity_registration_guard
BEFORE UPDATE OF max_students ON clubs
FOR EACH ROW WHEN NEW.max_students <
  (SELECT COALESCE(MAX(used), 0) FROM
    (SELECT COUNT(*) AS used FROM registrations r JOIN student_batch sb ON sb.student_id = r.student_id
     WHERE r.club_id = OLD.id GROUP BY sb.pool))
BEGIN
  SELECT RAISE(ABORT, 'club capacity below registrations');
END;
