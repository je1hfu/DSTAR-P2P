#ファイル名：p2p_core_prototype.py
#作成日：2025年3月23日
#目的：エリア内に参加局がいないかどうかを自動送信する機能と、自動送信に対して自動応答する機能を実装したプロトタイプのコード。これを核として、これから様々な機能を実装していきます。
#変更履歴：
#（20250325）BEACON_TEMPLATEを「Who's there?」から「CQ」に変更。視認性向上と電文短縮化が目的。
#（20250329）stations.dbというデータベースを実装し、応答局を自局ネットワーク圏内局として記憶できるようにした。
#（20250329）データベースの中身を確認できるよう、定期的に表示させる機能を追加した。
#（20250329）プログラム起動時にデータベースの中身を初期化する機能を追加した。
#（20250329）DBにすでに登録されている局から再度応答があった場合に、last_seen時間を最終更新日時に交信できるようにした。
#（20250329）DBに「詳細のデータ」を追加した。取り急ぎ位置情報としてグリッドロケーターを詳細のデータとする。
#（20250329）データベースの中から、詳細のデータを保持していない局と、詳細データの更新時間が5分以上前の局に対して、「QRV?」という詳細情報の問い合わせ機能を実装した。問い合わせ時間は3～15秒の間の3の倍数秒の任意の時間としている。

#（20250330）(1)CQフォーマットを "CQ de コールサイン" に修正
#（20250330）(2)CQの受信検知は "CQ de " で行い、コールサイン抽出
#（20250330）(3)応答記録は "自局 de 他局" のみに限定
#（20250330）(4)ログの視認性向上と目的コメント追加
#（20250401）QRV?に対して、"相手 de 自局 GL=xxxxx K" の形式で自動応答する機能を追加
#（20250401）QRV?に対する応答はランダムディレイである必要はないことに気が付いたので、その機能を削除した。
#（20250401）自局に対する詳細情報を受信した際に、DBを更新する機能を実装した。
#------------V0.3------------
#（20250401）データベースに「gl_updated_at」カラムを追加し、詳細データの最終更新日時を記録可能にした。これを活用することで、詳細データを最新の状態に保つためのトリガに使用することができるようになる。
#（20250401）QRV問合せによって詳細データを受信後、gl_updated_atカラムに最新更新日時を記録する仕様とした。この日時と現在時刻との差分が任意時間（とりあえず5分にした）経過したことを条件として、再度QRV問合せを実施する仕様にした。
#（20250401）QRV問合せ無限ループ防止のため、ネットワーク圏外判定用に使用しているカウンタ「query_count」を、詳細データを受信できた時点でリセットする仕様とした。これにより、正常に受信しているにもかかわらず、2回以上QRVした場合に詳細の再問合せをしなくなるバグを修正した。
#------------V0.4------------
#（20250406）応答メッセージにCRCを組み込み、冗長性を確保した。CRCはCRC32とし、検算不一致の場合は受信データを放棄する（再送要求はしない）。使用するライブラリはzlib。
#------------V0.5------------
#（20260319）コールサインや使用するポート（COM）、位置情報（GL）、ボーレートなどユーザー環境に依存する変数を本体のプログラムソースから切り離して、.envという環境設定ファイルを参照するように修正した。


#Config
import os
import serial
import time
import random
from datetime import datetime, timedelta
import threading
import sqlite3
from pathlib import Path
import pandas as pd
from tabulate import tabulate
import zlib #CRC計算用

try:
    from dotenv import load_dotenv
except ModuleNotFoundError as exc:
    raise RuntimeError(
        "python-dotenv is required. Install it with 'pip install python-dotenv'."
    ) from exc

# .env から公開したくないローカル設定を読み込む
ENV_PATH = Path(__file__).resolve().with_name(".env")

if not ENV_PATH.is_file():
    raise RuntimeError(
        f"Missing .env file: {ENV_PATH}. "
        "Set DSTAR_PORT, DSTAR_BAUD_RATE, DSTAR_CALLSIGN, and DSTAR_MY_GL."
    )

load_dotenv(dotenv_path=ENV_PATH)


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(
            f"Missing required environment variable '{name}' in {ENV_PATH}. "
            "Update your .env file."
        )
    return value.strip()


PORT = get_required_env("DSTAR_PORT")

try:
    BAUD_RATE = int(get_required_env("DSTAR_BAUD_RATE"))
except ValueError as exc:
    raise RuntimeError(
        f"Invalid DSTAR_BAUD_RATE in {ENV_PATH}. Use an integer such as 9600."
    ) from exc

CALLSIGN = get_required_env("DSTAR_CALLSIGN")
MY_GL = get_required_env("DSTAR_MY_GL")
BEACON_TEMPLATE = f"CQ de {CALLSIGN}"
RESPONSE_DELAYS = [3, 6, 9, 12, 15]
BEACON_INTERVAL_RANGE = (30, 90)
QRV_DELAYS = [2, 4, 6, 8, 10]

ser = serial.Serial(PORT, BAUD_RATE, timeout=1)

#データベース初期化
conn = sqlite3.connect("stations.db", check_same_thread=False)
cursor = conn.cursor()

# スキーマ拡張：gl_updated_at を追加
cursor.execute("""
CREATE TABLE IF NOT EXISTS stations (
    callsign TEXT PRIMARY KEY,
    last_seen TEXT,
    status TEXT,
    gl TEXT,
    gl_updated_at TEXT,
    query_count INTEGER DEFAULT 0
)
""")
conn.commit()
cursor.execute("DELETE FROM stations")
conn.commit()
print("🧹 データベースを初期化しました（gl_updated_atカラム付き）")

#CRCを付加する関数。入力が「Something」なら、戻り値は「Something CRC=ABCDEF12」のようになる。
def add_crc(message: str) -> str: #入力をstring型で受けて、戻り値もstring型とすることを指定している。（数値が入力されたときにinteger型で入ってしまうと、CRCがうまくいかないので。）
    stripped_message = message.strip()
    crc = zlib.crc32(stripped_message.encode()) & 0xffffffff #message.encode()は入力「message」をバイナリに変換するための関数。バイナリにしてcrc32関数に渡すことで、crc32は戻り値を返せる。　"& 0xffffffff"は符号なしの整数で返すようにするためのおまじない（これが無いとマイナスの値を返すことがある）
    return f"{stripped_message} CRC={crc:08X}\n"

#CRCを検証する関数
def verify_crc(line: str) -> tuple[bool, str]:
    if "CRC=" in line:
        try:
            parts = line.rsplit("CRC=", 1)
            content = parts[0].strip()
            received_crc = parts[1].split()[0].strip()
            calculated_crc = f"{zlib.crc32(content.encode()) & 0xffffffff:08X}"
            if calculated_crc != received_crc:
                print(f"CRC不一致。詳細→計算値は{calculated_crc}, 受信値は{received_crc}")
            return (calculated_crc == received_crc, content)
        except Exception as e:
            print(f"CRC解析エラー： {e}")
            return (False, "")
    return (True, line)

#自動CQ送信
def auto_beacon():
    count = 1
    while True:
        message = f"{BEACON_TEMPLATE}\n"
        final_message = add_crc(message.strip())
        print(f"🔵送信（ビーコン）：'{final_message.strip()}'")
        ser.write(final_message.encode())
        wait = random.randint(*BEACON_INTERVAL_RANGE)
        print(f"⏳ 次回送信まで: {wait}秒")
        time.sleep(wait)
        count += 1

responded_callsigns = set()

#受信処理と送信処理（CQ応答とQRV応答）
def listen_and_respond():
    local_cursor = conn.cursor()
    while True:
        try:
            raw = ser.readline().decode(errors='ignore').strip()

            #データを受信していないときはスキップする処理
            if not raw:
                continue

            if raw:
                print(f"🟢 受信（生データ）: '{raw}'")

            ok, line = verify_crc(raw)

            if not ok:
                print(f"🔴 CRC検証失敗！受信： '{raw}', CRC計算した結果は： '{line}'")
                continue
            else:
                print(f"CRC検証成功！内容： '{line}'")
            if line:
                recv_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"📥 [{recv_time}] 受信: {line}")

                #CQ応答部分
                if line.startswith("CQ de ") and CALLSIGN not in line:
                    sender = line.split("CQ de ")[1].strip()
                    delay = random.choice(RESPONSE_DELAYS)
                    print(f"🤖 {sender} にCQ応答準備中（{delay}秒後）")
                    time.sleep(delay)
                    response_message = f"{sender} de {CALLSIGN}\n"
                    final_message = add_crc(response_message.strip())
                    print(f"🔵送信（CQ応答）：'{final_message.strip()}'")
                    ser.write(final_message.encode())
                #QRV応答部分
                if line.startswith(f"QRV? {CALLSIGN} de "):
                    try:
                        parts = line.split(f"QRV? {CALLSIGN} de ")
                        if len(parts) == 2:
                            target = parts[0].strip()
                            from_call = parts[1].strip()
                            print(f"📍 {from_call} からのQRV?にGL応答送信中")
                            gl_response = f"{from_call} de {CALLSIGN} GL={MY_GL} K\n"
                            final_message = add_crc(gl_response.strip())
                            print(f"🔵送信（GL応答）：'{final_message.strip()}'")
                            ser.write(final_message.encode())
                    except Exception as err:
                        print(f"⚠️ QRV応答処理エラー: {err}")

                #応答記録（自局あてのみ）
                if line.startswith(f"{CALLSIGN} de "):
                    sender = line.split("de ")[1].split()[0].strip()
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("""
INSERT INTO stations (callsign, last_seen, status)
VALUES (?, ?, ?)
ON CONFLICT(callsign) DO UPDATE SET last_seen=excluded.last_seen
""", (sender, now, "active"))
                    conn.commit()
                    if sender not in responded_callsigns:
                        responded_callsigns.add(sender)
                        print(f"📝 応答局を記録: {sender}")

                # GL応答の処理（厳密化＋受信日時も記録）
                if "GL=" in line and line.endswith("K"):
                    try:
                        gl = line.split("GL=")[1].split()[0].strip()
                        header = line.split("GL=")[0].strip()
                        parts = header.split("de")
                        if len(parts) == 2:
                            to_callsign = parts[0].strip()
                            from_callsign = parts[1].strip()
                            if to_callsign == CALLSIGN:
                                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                cursor.execute("""
UPDATE stations SET gl = ?, gl_updated_at = ?, query_count = 0 WHERE callsign = ?
""", (gl, now, from_callsign))
                                conn.commit()
                                print(f"✅ GL情報をDBに保存（カウントリセット）: {from_callsign} → {gl}（{now}）")
                            else:
                                print(f"🔎 GL応答は自局宛てではないため無視: {line}")
                    except Exception as e:
                        print(f"⚠️ GL受信処理エラー: {e}")

        except Exception as e:
            print(f"⚠️ 受信中エラー: {e}")

#詳細問合せ処理（詳細データの最終更新から5分以上経過でQRV問合せ）
def query_for_details():
    local_cursor = conn.cursor()
    while True:
        five_min_ago = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        local_cursor.execute("""
SELECT callsign, query_count FROM stations
WHERE (gl IS NULL OR gl_updated_at IS NULL OR gl_updated_at < ?)
""", (five_min_ago,))
        rows = local_cursor.fetchall()
        for row in rows:
            callsign, q_count = row
            if q_count >= 2:
                continue
            delay = random.choice(QRV_DELAYS)
            print(f"📡 GL再問い合わせ準備中: {callsign}（{delay}秒後）")
            time.sleep(delay)
            message = f"QRV? {callsign} de {CALLSIGN}\n"
            final_message = add_crc(message.strip())
            print(f"🔵送信（QRV問合せ）：'{final_message.strip()}'")
            ser.write(final_message.encode())
            local_cursor.execute("UPDATE stations SET query_count = query_count + 1 WHERE callsign = ?", (callsign,))
            conn.commit()
        time.sleep(20)

#DB表示機能
def display_station_list():
    local_cursor = conn.cursor()
    while True:
        local_cursor.execute("SELECT * FROM stations")
        rows = local_cursor.fetchall()
        if rows:
            df = pd.DataFrame(rows, columns=["callsign", "last_seen", "status", "gl", "gl_updated_at", "query_count"])
            print("📋 現在の登録局一覧:")
            print(tabulate(df, headers='keys', tablefmt='grid'))
        else:
            print("📋 現在の登録局はありません。")
        print("-" * 40)
        time.sleep(10)

#Main
try:
    print("🚀 P2Pコア・プロトタイプ（GL再照会機能付き）起動中（Ctrl+Cで終了）")
    threading.Thread(target=auto_beacon, daemon=True).start()
    threading.Thread(target=listen_and_respond, daemon=True).start()
    threading.Thread(target=display_station_list, daemon=True).start()
    threading.Thread(target=query_for_details, daemon=True).start()

    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\n🛑 ユーザーにより終了されました")

finally:
    if ser.is_open:
        ser.close()
        print("🔌 シリアルポートを閉じました")
    conn.close()
    print("🗃️ データベースを閉じました")
