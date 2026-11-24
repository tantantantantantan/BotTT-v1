Ticket Assistant v3.1.1 Fast Smart - TESTER Builder
Expire: 30/09/2026

วิธีทำ Tester build:
1) แตก ZIP นี้ไว้ในเครื่อง Owner เท่านั้น
2) ดับเบิลคลิก generate_tester_license_2026-09-30.bat
3) ตรวจ license.json ว่า expire_date = 2026-09-30
4) ดับเบิลคลิก build_exe.bat
5) เมื่อ Build ผ่าน ให้ส่งเฉพาะไฟล์ในโฟลเดอร์ dist ให้ Tester

หรือใช้ build_tester_all.bat เพื่อ Generate License + Build ต่อกัน

สำคัญ:
- ห้ามส่งโฟลเดอร์ owner_tools ให้ Tester
- ห้ามส่ง private_key.pem หรือ generate_license.py ให้ Tester
- Tester ใช้ run_chrome.bat -> Login TTM -> เปิด TicketAssistant_v3_1_1_TESTER.exe
- โปรแกรมหยุดก่อน Payment
- การตรวจ visitor presence / anti-bot ให้ผู้ใช้กดเอง
