"""The desktop app's text in Thai (default) and English.

    tr("Start backup")                       -> "เริ่มสำรองข้อมูล" (Thai) or the English text
    tr("{n} files left", n=f"{12:,}")        -> the text with the values filled in

The English text is the key. A text missing from TH is shown in English, so nothing breaks while
a translation is missing. tr_progress() and tr_error() translate the progress lines and the error
messages that come from logs_backup.py (the log itself stays in English, for support and audit).
"""
import re

LANGUAGES = {"th": "ไทย", "en": "English"}
_lang = "th"


def set_language(code):
    global _lang
    _lang = code if code in LANGUAGES else "th"


def language():
    return _lang


def tr(text, **values):
    s = TH.get(text, text) if _lang == "th" else text
    return s.format(**values) if values else s


TH = {
    # window, navigation
    "Finisar Log Backup": "Finisar Log Backup",
    "MOCHI archives": "สำรองไฟล์ log ด้วย MOCHI",
    "Backup": "สำรองข้อมูล",
    "History": "ประวัติ",
    "Settings": "การตั้งค่า",
    "Tools": "เครื่องมือ",
    "Logs: {source}\nArchive: {output}": "ไฟล์ log: {source}\nที่เก็บ: {output}",

    # steps
    "Finish the previous run": "ทำงานที่ค้างจากครั้งก่อนให้เสร็จ",
    "Ask SQL Server for barcodes": "ดึงรายการบาร์โค้ดจาก SQL Server",
    "Scan the log folders": "สแกนโฟลเดอร์ log",
    "Stage files": "เตรียมไฟล์ (staging)",
    "Commit, verify, index": "บันทึกเข้าไฟล์เก็บถาวร ตรวจสอบ และทำดัชนี",

    # Backup page
    "Archive test-station logs into monthly MOCHI archives.":
        "เก็บไฟล์ log ของสถานีทดสอบเข้าไฟล์เก็บถาวร MOCHI รายเดือน",
    "A · Continue from the last run": "A · ทำต่อจากครั้งล่าสุด",
    "Picks up where the last successful run ended, up to today minus the lookback days. "
    "Use this for the regular backup.":
        "เริ่มจากจุดที่การสำรองครั้งล่าสุดที่สำเร็จจบไว้ จนถึงวันนี้ลบด้วยจำนวนวันที่เว้นไว้ "
        "ใช้สำหรับการสำรองข้อมูลตามปกติ",
    "B · Custom date range": "B · กำหนดช่วงวันที่เอง",
    "Back-fill or redo any range of dates. Does not change where Continue starts.":
        "สำรองย้อนหลังหรือทำซ้ำช่วงวันที่ใดก็ได้ ไม่มีผลต่อจุดเริ่มของโหมด A",
    "From": "ตั้งแต่",
    "to": "ถึง",
    "Preview": "ดูตัวอย่าง",
    "Start backup": "เริ่มสำรองข้อมูล",
    "Progress": "ความคืบหน้า",
    "Stop": "หยุด",
    "Stopping…": "กำลังหยุด…",
    "Stops at the next safe point. Nothing is lost: the next run continues.":
        "หยุดที่จุดที่ปลอดภัยถัดไป ไม่มีข้อมูลสูญหาย ครั้งหน้าจะทำต่อจากจุดนี้",
    "Log": "บันทึกการทำงาน (log)",
    "Fill in Settings first": "กรุณากรอกการตั้งค่าก่อน",
    "the beginning": "เริ่มต้น",
    "(start)": "(เริ่มต้น)",
    "Elapsed {time}": "ใช้เวลา {time}",
    "B · custom range": "B · ช่วงวันที่กำหนดเอง",
    "A · continue": "A · ทำต่อ",
    "Stopped run ({kind}): continue where it left off": "งานที่หยุดไว้ ({kind}): ทำต่อจากจุดที่หยุด",
    "<b>{left} of {total} files left</b> · dates {start} → {end} · stopped {when}.<br>Continue picks up "
    "exactly there, without asking SQL Server or scanning the log folders again. Files staged before the stop "
    "are checked and finished first.":
        "<b>เหลือ {left} จาก {total} ไฟล์</b> · วันที่ {start} → {end} · หยุดเมื่อ {when}<br>กด ทำต่อ "
        "เพื่อเริ่มจากจุดเดิมทันที โดยไม่ต้องถาม SQL Server หรือสแกนโฟลเดอร์ log ใหม่ "
        "ไฟล์ที่เตรียมไว้ก่อนหยุดจะถูกตรวจสอบและทำให้เสร็จก่อน",
    "Discard": "ยกเลิกงานนี้",
    "Continue": "ทำต่อ",
    "Forget this run's plan. Its files stay in the log folders; a later run finds them.":
        "ลืมแผนของงานนี้ ไฟล์ยังอยู่ในโฟลเดอร์ log ครบ การสำรองครั้งต่อไปจะพบไฟล์เหล่านั้นเอง",
    "Discard the stopped run?": "ยกเลิกงานที่หยุดไว้?",
    "Forget the plan of this stopped run? Nothing is deleted: its files stay in the log folders and a later "
    "run finds them (after a new scan).":
        "ลืมแผนของงานที่หยุดไว้นี้? ไม่มีไฟล์ใดถูกลบ ไฟล์ยังอยู่ในโฟลเดอร์ log "
        "และการสำรองครั้งต่อไปจะพบไฟล์เหล่านั้น (หลังสแกนใหม่)",
    "Cannot discard": "ยกเลิกไม่ได้",
    "A stopped run can continue": "มีงานที่หยุดไว้ซึ่งทำต่อได้",
    "The stopped continue run still has {left} files to archive.\n\nContinue it (no new scan), or start a new "
    "continue run? The new run covers its dates and replaces it (its files are found again by the new scan). "
    "Stopped custom-range runs are kept either way.":
        "งานโหมด A ที่หยุดไว้ยังเหลือ {left} ไฟล์ที่ต้องเก็บ\n\nจะทำงานนั้นต่อ (ไม่ต้องสแกนใหม่) "
        "หรือเริ่มงานโหมด A ใหม่? งานใหม่ครอบคลุมวันที่เดียวกันและจะแทนที่งานเดิม "
        "(ไฟล์จะถูกพบอีกครั้งจากการสแกนใหม่) งานโหมด B ที่หยุดไว้จะยังเก็บไว้เสมอ",
    "Continue stopped run": "ทำงานที่หยุดไว้ต่อ",
    "Start new run": "เริ่มงานใหม่",
    "Cancel": "ยกเลิก",
    "OK": "ตกลง",
    "Yes": "ใช่",
    "No": "ไม่",
    "Save": "บันทึก",
    "Don't save": "ไม่บันทึก",
    "Settings needed": "ต้องตั้งค่าก่อน",
    "Set {name} on the Settings page first.": "กรุณาตั้งค่า {name} ในหน้าการตั้งค่าก่อน",
    "Settings problem": "การตั้งค่ามีปัญหา",
    "Date range": "ช่วงวันที่",
    "The date range starts after it ends.": "วันที่เริ่มต้องไม่อยู่หลังวันที่สิ้นสุด",
    "Backup: {mode}": "สำรองข้อมูล: {mode}",
    "Preview: {mode}": "ดูตัวอย่าง: {mode}",
    "continue": "ทำต่อ",
    "continue the stopped run {start} → {end}": "ทำงานที่หยุดไว้ต่อ {start} → {end}",
    "{files} files committed ({barcodes} barcodes) · {errors} errors":
        "บันทึกเข้าไฟล์เก็บถาวรแล้ว {files} ไฟล์ ({barcodes} บาร์โค้ด) · ข้อผิดพลาด {errors}",
    " · {n} files from the previous run": " · {n} ไฟล์จากงานครั้งก่อน",
    "<b>Done.</b> {counts}. Committed is not a backup on its own: keep an export on another drive (Tools).":
        "<b>เสร็จแล้ว</b> {counts} การบันทึกเข้าไฟล์เก็บถาวรยังไม่ใช่สำเนาสำรองที่สมบูรณ์ "
        "ควรส่งออกสำเนาไว้อีกไดรฟ์หนึ่งด้วย (เครื่องมือ)",
    "<b>Stopped</b> with {left} files left. {counts}. Nothing is lost: you can close the program or shut down "
    "the PC, and run other backups meanwhile. Continue it any time with its <b>Continue</b> button above.":
        "<b>หยุดแล้ว</b> เหลืออีก {left} ไฟล์ {counts} ไม่มีข้อมูลสูญหาย ปิดโปรแกรมหรือปิดเครื่องได้เลย "
        "และระหว่างนี้สำรองงานอื่นได้ กดปุ่ม <b>ทำต่อ</b> ด้านบนเมื่อพร้อม",
    "Cancelled. Nothing was moved or recorded.": "ยกเลิกแล้ว ไม่มีไฟล์ถูกย้ายหรือบันทึก",
    "none": "ไม่มี",
    "<b>Preview.</b> {candidates} candidate barcodes, {scanned} files scanned, <b>{files} files would be "
    "archived</b> ({months}). Nothing was moved.":
        "<b>ตัวอย่าง</b> บาร์โค้ดที่เข้าเงื่อนไข {candidates} รายการ สแกน {scanned} ไฟล์ "
        "<b>จะเก็บเข้าไฟล์เก็บถาวร {files} ไฟล์</b> ({months}) ยังไม่มีไฟล์ใดถูกย้าย",
    "Free space": "พื้นที่ว่าง",
    "<b>Failed:</b> {message}<br>Nothing is lost: files not committed yet are still in the log folders or in "
    "staging. Start again to continue. Details: Tools → Open log file.":
        "<b>ไม่สำเร็จ:</b> {message}<br>ไม่มีข้อมูลสูญหาย ไฟล์ที่ยังไม่ได้บันทึกยังอยู่ในโฟลเดอร์ log "
        "หรือในพื้นที่ staging เริ่มใหม่อีกครั้งเพื่อทำต่อ รายละเอียด: เครื่องมือ → เปิดไฟล์ log",
    "stop requested: stopping at the next safe point (a commit in progress is finished first)":
        "stop requested: stopping at the next safe point (a commit in progress is finished first)",

    # free space rows
    "{what}: <b>{free}</b> free, needs {need}": "{what}: ว่าง <b>{free}</b> ต้องใช้ {need}",
    "unknown": "ไม่ทราบ",
    "staging (this batch at a time)": "staging (ครั้งละหนึ่งชุด)",
    "archives (at most: MOCHI compresses)": "ไฟล์เก็บถาวร (สูงสุด: MOCHI บีบอัดให้เล็กลง)",
    "index copy on this PC": "สำเนาดัชนีบนเครื่องนี้",

    # confirm dialog
    "Archive these files?": "เก็บไฟล์เหล่านี้เข้าไฟล์เก็บถาวร?",
    "<b>{files} files</b> ({barcodes} barcodes, {size}) will be archived into {months} monthly archive(s), "
    "then removed from the log folders.":
        "<b>{files} ไฟล์</b> ({barcodes} บาร์โค้ด, {size}) จะถูกเก็บเข้าไฟล์เก็บถาวรรายเดือน {months} ไฟล์ "
        "แล้วจึงลบออกจากโฟลเดอร์ log",
    "Month": "เดือน",
    "Barcodes": "บาร์โค้ด",
    "Files": "ไฟล์",
    "Size": "ขนาด",
    "Archive": "เก็บเข้าไฟล์เก็บถาวร",

    # History page
    "Every run recorded in the index, newest first.": "ทุกงานที่บันทึกไว้ในดัชนี เรียงจากล่าสุด",
    "Refresh": "รีเฟรช",
    "#": "#",
    "Started": "เริ่มเมื่อ",
    "Status": "สถานะ",
    "Mode": "โหมด",
    "Errors": "ข้อผิดพลาด",
    "No runs yet.": "ยังไม่มีการสำรองข้อมูล",
    "Cannot read the index: {error}": "อ่านดัชนีไม่ได้: {error}",
    "{n} run(s) · {path}": "{n} งาน · {path}",
    "success": "สำเร็จ",
    "failed": "ไม่สำเร็จ/หยุด",
    "running": "กำลังทำงาน",
    "resumed": "ทำต่อจากที่ค้าง",
    "custom": "กำหนดเอง",
    " · can continue": " · ทำต่อได้",

    # Settings page
    "Saved for this Windows user in {path}": "บันทึกไว้สำหรับผู้ใช้ Windows คนนี้ที่ {path}",
    "Folders": "โฟลเดอร์",
    "Log folder (source)": "โฟลเดอร์ log (ต้นทาง)",
    "Archive folder": "โฟลเดอร์เก็บถาวร",
    "Index file": "ไฟล์ดัชนี",
    "D:/logs  or  //server/share/logs": "D:/logs  หรือ  //server/share/logs",
    "default: <archive folder>/index.db": "ค่าเริ่มต้น: <โฟลเดอร์เก็บถาวร>/index.db",
    "Subfolders to scan": "โฟลเดอร์ย่อยที่สแกน",
    "File extensions": "นามสกุลไฟล์",
    "Also scan folders inside each subfolder": "สแกนโฟลเดอร์ที่อยู่ข้างในโฟลเดอร์ย่อยด้วย",
    "Leave the newest": "เว้นไฟล์ล่าสุดไว้",
    " days": " วัน",
    "Read only: one query returns the barcodes of the date range.":
        "อ่านอย่างเดียว: ใช้คำสั่งค้นหาครั้งเดียวเพื่อดึงบาร์โค้ดของช่วงวันที่",
    "sqlhost  or  HOST\\INSTANCE": "sqlhost  หรือ  HOST\\INSTANCE",
    "1433 (default)": "1433 (ค่าเริ่มต้น)",
    "asked when a run starts, unless remembered": "จะถามเมื่อเริ่มงาน หากไม่ได้ให้จำไว้",
    "remembered (type to change)": "จำไว้แล้ว (พิมพ์ใหม่เพื่อเปลี่ยน)",
    "Remember the password (encrypted for this Windows user)": "จำรหัสผ่าน (เข้ารหัสสำหรับผู้ใช้ Windows คนนี้)",
    "Only available on Windows": "ใช้ได้เฉพาะบน Windows",
    "Request (default)": "ขอเข้ารหัส (ค่าเริ่มต้น)",
    "Require": "บังคับเข้ารหัส",
    "Off (trusted network only)": "ปิด (เฉพาะเครือข่ายที่เชื่อถือได้)",
    "Server": "เซิร์ฟเวอร์",
    "Port": "พอร์ต",
    "Database": "ฐานข้อมูล",
    "User": "ผู้ใช้",
    "Password": "รหัสผ่าน",
    "Encryption": "การเข้ารหัส",
    "Network drive and speed": "ไดรฟ์เครือข่ายและความเร็ว",
    "Automatic suits most setups: on a network drive files are copied to local staging, 16 at a time; on a "
    "local disk they are moved one at a time.":
        "แบบอัตโนมัติเหมาะกับการใช้งานส่วนใหญ่: บนไดรฟ์เครือข่ายจะคัดลอกไฟล์มาพักบนเครื่องนี้ครั้งละ 16 ไฟล์ "
        "บนดิสก์ในเครื่องจะย้ายไฟล์ทีละไฟล์",
    "Automatic": "อัตโนมัติ",
    "Copy to local staging (network drive)": "คัดลอกมาพักบนเครื่องนี้ (ไดรฟ์เครือข่าย)",
    "Move by rename (local disk)": "ย้ายด้วยการเปลี่ยนชื่อ (ดิสก์ในเครื่อง)",
    "automatic": "อัตโนมัติ",
    " files": " ไฟล์",
    "Stop waits for at most one batch's commit. Smaller: quicker stop, more commits.":
        "การหยุดจะรอการบันทึกของชุดปัจจุบันให้เสร็จก่อน ชุดเล็กลง: หยุดได้เร็วขึ้น แต่บันทึกบ่อยขึ้น",
    "Staging": "การพักไฟล์ (staging)",
    "Staging folder": "โฟลเดอร์พักไฟล์",
    "Files at once": "จำนวนไฟล์พร้อมกัน",
    "Batch size": "ขนาดชุด",
    "Other": "อื่น ๆ",
    "found automatically": "ค้นหาอัตโนมัติ",
    "optional: http://host:port": "ไม่บังคับ: http://host:port",
    "optional: a@b.c": "ไม่บังคับ: a@b.c",
    "mochi program": "โปรแกรม mochi",
    "Email report API": "API ส่งรายงานทางอีเมล",
    "Email report to": "ส่งรายงานไปที่อีเมล",
    "Language": "ภาษา",
    "The language changes the next time the program starts.": "ภาษาจะเปลี่ยนเมื่อเปิดโปรแกรมครั้งถัดไป",
    "Import config.json…": "นำเข้า config.json…",
    "Save settings": "บันทึกการตั้งค่า",
    "Unsaved changes": "มีการแก้ไขที่ยังไม่บันทึก",
    "Saved": "บันทึกแล้ว",
    "Missing setting": "ยังขาดการตั้งค่า",
    "Please set {name}.": "กรุณาตั้งค่า{name}",
    "the log folder": "โฟลเดอร์ log",
    "the archive folder": "โฟลเดอร์เก็บถาวร",
    "the index file": "ไฟล์ดัชนี",
    "Cannot save": "บันทึกไม่ได้",
    "Unsaved settings": "การตั้งค่ายังไม่ได้บันทึก",
    "Save the changed settings before starting?": "บันทึกการตั้งค่าที่แก้ไขก่อนเริ่มหรือไม่?",
    "Import config.json": "นำเข้า config.json",
    "Cannot import": "นำเข้าไม่ได้",
    "Imported: check and save": "นำเข้าแล้ว: ตรวจสอบแล้วกดบันทึก",
    "Imported from config.json: check and save": "นำเข้าจาก config.json แล้ว: ตรวจสอบแล้วกดบันทึก",
    "Browse…": "เลือก…",
    "Choose a folder": "เลือกโฟลเดอร์",
    "Choose a file": "เลือกไฟล์",

    # Tools page
    "Check the setup": "ตรวจสอบการตั้งค่า",
    "Finds the mochi program and makes one SQL Server connection. Changes nothing.":
        "ค้นหาโปรแกรม mochi และทดลองเชื่อมต่อ SQL Server หนึ่งครั้ง ไม่มีการเปลี่ยนแปลงใด ๆ",
    "Run check": "เริ่มตรวจสอบ",
    "Checking…": "กำลังตรวจสอบ…",
    "Settings: {error}": "การตั้งค่า: {error}",
    "network drive": "ไดรฟ์เครือข่าย",
    "local disk": "ดิสก์ในเครื่อง",
    "Log folder: {path} ({where}), {threads} file(s) at once":
        "โฟลเดอร์ log: {path} ({where}) ทำงานพร้อมกัน {threads} ไฟล์",
    "Staging: {mode}, in {path} ({where})": "การพักไฟล์: {mode} ที่ {path} ({where})",
    "copy": "คัดลอก",
    "move": "ย้าย",
    "Archives: {path} ({where})": "ไฟล์เก็บถาวร: {path} ({where})",
    "Free space for {name}: {free}": "พื้นที่ว่างสำหรับ{name}: {free}",
    "unknown (not reachable)": "ไม่ทราบ (เข้าถึงไม่ได้)",
    "staging": "การพักไฟล์",
    "archives": "ไฟล์เก็บถาวร",
    "index": "ดัชนี",
    "Index: {path} ({where})": "ดัชนี: {path} ({where})",
    "; checked and copied to {path} before each run": " ตรวจสอบและคัดลอกไปที่ {path} ก่อนทุกครั้งที่ทำงาน",
    "Export a restorable copy": "ส่งออกสำเนาสำหรับกู้คืน",
    "Copies the archives, the index and the settings (without the password) to another drive, every file "
    "checked. Archives unchanged since the last export are skipped.":
        "คัดลอกไฟล์เก็บถาวร ดัชนี และการตั้งค่า (ไม่รวมรหัสผ่าน) ไปยังไดรฟ์อื่น โดยตรวจสอบทุกไฟล์ "
        "ไฟล์เก็บถาวรที่ไม่เปลี่ยนตั้งแต่การส่งออกครั้งก่อนจะข้ามไป",
    "Export to…": "ส่งออกไปที่…",
    "Busy": "กำลังทำงาน",
    "Wait for the running backup to end first.": "กรุณารอให้การสำรองข้อมูลที่กำลังทำงานเสร็จก่อน",
    "Export to (another drive)": "ส่งออกไปที่ (ไดรฟ์อื่น)",
    "Exporting…": "กำลังส่งออก…",
    "{item}: {files} files, {size}, {copied} copied now": "{item}: {files} ไฟล์, {size}, คัดลอกครั้งนี้ {copied}",
    "Exported to {target}. {parts}. How to restore: RESTORE.txt in that folder.":
        "ส่งออกไปที่ {target} แล้ว {parts} วิธีกู้คืน: ดูไฟล์ RESTORE.txt ในโฟลเดอร์นั้น",
    "Export failed: {error}": "ส่งออกไม่สำเร็จ: {error}",
    "config": "การตั้งค่า",
    "Files ": "ไฟล์",
    "Open log file": "เปิดไฟล์ log",
    "Open settings folder": "เปิดโฟลเดอร์การตั้งค่า",

    # main window
    "SQL Server password": "รหัสผ่าน SQL Server",
    "Password for {user} on {server}:": "รหัสผ่านของ {user} บน {server}:",
    "A backup is running": "กำลังสำรองข้อมูลอยู่",
    "Stop the backup and close when it has stopped?\nNothing is lost: the next run continues where it stopped.":
        "หยุดการสำรองข้อมูลแล้วปิดโปรแกรมเมื่อหยุดเสร็จหรือไม่?\nไม่มีข้อมูลสูญหาย ครั้งหน้าจะทำต่อจากจุดที่หยุด",
}

# Progress lines from logs_backup.py (report messages): pattern -> Thai template.
_PROGRESS = [
    (r"^batch (\d+)/(\d+) ?", r"ชุดที่ \1/\2 "),
    (r"copying to local staging", "กำลังคัดลอกมาพักบนเครื่องนี้"),
    (r"finishing the batch an earlier run left in staging", "กำลังทำชุดที่ค้างจากครั้งก่อนให้เสร็จ"),
    (r"^([\d,]+) files$", r"\1 ไฟล์"),
    (r"^([\d,]+) candidate barcodes$", r"บาร์โค้ดที่เข้าเงื่อนไข \1 รายการ"),
    (r"^([\d,]+) matches$", r"พบ \1 ไฟล์ที่ตรงกัน"),
    (r"committing ", "กำลังบันทึก "),
    (r" cleaning up$", " กำลังเก็บกวาด"),
    (r"^continuing the stopped run (.*)$", r"กำลังทำงานที่หยุดไว้ต่อ \1"),
    (r"^([\d,]+) files left \(kept from the first scan\)$", r"เหลือ \1 ไฟล์ (จากการสแกนครั้งแรก)"),
    (r"\(start\)", "(เริ่มต้น)"),
]

# Error messages from logs_backup.py: pattern -> a Thai headline; the English detail follows it.
_ERRORS = [
    (r"not enough free space", "พื้นที่ว่างไม่พอ ยังไม่มีไฟล์ใดถูกย้าย"),
    (r"cannot be reached", "เข้าถึงโฟลเดอร์ไม่ได้ (ไดรฟ์เครือข่ายยังไม่เชื่อมต่อ?) ไม่มีการเปลี่ยนแปลงใด ๆ"),
    (r"looks damaged", "ไฟล์ดัชนีอาจเสียหาย ไม่มีการเปลี่ยนแปลงใด ๆ ให้กู้คืนจากสำเนาที่ระบุไว้"),
    (r"another backup run is in progress", "มีการสำรองข้อมูลอื่นทำงานอยู่ กรุณาลองใหม่เมื่องานนั้นเสร็จ"),
    (r"mochi program was not found", "ไม่พบโปรแกรม mochi กรุณาตั้งค่าตำแหน่งในหน้าการตั้งค่า"),
    (r"SQL Server|login failed|20009|18456|4060", "เชื่อมต่อ SQL Server ไม่สำเร็จ"),
    (r"month\(s\) failed", "บางเดือนบันทึกไม่สำเร็จ ไฟล์ยังอยู่ในพื้นที่ staging สำหรับครั้งถัดไป"),
    (r"date range", "ช่วงวันที่ไม่ถูกต้อง"),
    (r"cannot create", "สร้างโฟลเดอร์ไม่ได้"),
    (r"must be on the same drive", "โฟลเดอร์ staging ต้องอยู่ไดรฟ์เดียวกับโฟลเดอร์ log"),
]


def tr_progress(message):
    """A progress line from logs_backup.py, in the app's language."""
    if _lang != "th" or not message:
        return message
    for pattern, thai in _PROGRESS:
        message = re.sub(pattern, thai, message)
    return message


def tr_error(message):
    """An error from logs_backup.py: a Thai headline, then the original detail (for support)."""
    if _lang != "th":
        return message
    for pattern, thai in _ERRORS:
        if re.search(pattern, message, re.IGNORECASE):
            return f"{thai}<br><span style='color:#64748b'>({message})</span>"
    return message
