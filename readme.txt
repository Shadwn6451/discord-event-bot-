本機啟動步驟（Windows/macOS/Linux）

1.安裝 Python（建議 3.11），安裝時勾選「Add Python to PATH」（Windows）。

2.終端機進到專案資料夾：cd discord-event-bot

3.安裝套件：pip install -r requirements.txt

4.放 Token（二擇一）
環境變數：
Windows PowerShell：setx DISCORD_TOKEN "你的BotToken"
或者，文字檔：在專案同層建立 DISCORD_TOKEN.txt，第一行貼上 token。

6.執行：python main.py

會看到：Bot 已登入為 ...就成功了。

到你的伺服器測 !ping、!create_event ...
