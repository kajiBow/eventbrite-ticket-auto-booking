"""
Eventbrite チケット自動予約システム(最終盤)

このプログラムは以下の機能を提供します：
1. Eventbrite APIを使用してチケットの空き状況を定期的に監視
2. 空き枠が見つかった場合、Seleniumを使用して自動予約
3. ログイン情報をCookieとして保存し、次回以降の実行で再利用

使い方：
1. 初回実行時：ブラウザが開くのでEventbriteにログイン
2. ログイン後、Enterキーを押すと監視開始
3. 空き枠が見つかると自動的に予約処理を実行
4. 最後のチェックアウトは手動で完了

注意事項：
- POLL_INTERVALは1.0秒以下にしないこと（API制限によるBANのリスクあり）
- ブラウザは監視中も開いたままにしておくこと
"""

import requests
import time
import webbrowser  # デフォルトブラウザでURLを開くために使用
import json
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# ============================================================
# 設定項目
# ============================================================

# イベントID（EventbriteのイベントURLから取得）
EVENT_ID = '1247015881069'

# EventbriteのAPIトークン（.envファイルから読み込み）
API_TOKEN = os.getenv('API_TOKEN')

# 特定のチケットクラスIDを監視する場合は指定（通常はNoneでOK）
TARGET_TICKET_CLASS_ID = None

# チケット確認の間隔（秒）
# 注意：1.0秒以下にするとAPI制限に引っかかりBANされる可能性があります（計算上1.8がlimit）
POLL_INTERVAL = 1.8

# チケット購入ページのベースURL
CHECKOUT_BASE_URL = f'https://www.eventbrite.com/e/{EVENT_ID}'

# API応答をJSONファイルに保存するかどうか（1: 保存する, 0: 保存しない）
SAVE_JSON_RESPONSE = 1

# 並列取得を有効化するかどうか（1: 有効, 0: 無効）
# 有効にすると高速ですが、API負荷が高くなります
ENABLE_PARALLEL_FETCH = 1

# 在庫発見時に即座に終了するかどうか（1: 即座に終了, 0: すべてのページを取得）
EARLY_EXIT = 0

# 並列リクエストの最大数
# 多いほど高速ですが、API制限に注意が必要です
MAX_WORKERS = 10

# Discord Webhook URL（.envファイルから読み込み）
# 通知が不要な場合は .env ファイルで空欄にしておく
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL') or None

# ============================================================
# API設定
# ============================================================

# Eventbrite API のベースURL
BASE_URL = 'https://www.eventbriteapi.com/v3'

# HTTPセッション（コネクションを再利用して高速化）
session = requests.Session()
session.headers.update({
    'Authorization': f'Bearer {API_TOKEN}',
    'Content-Type': 'application/json'
})

# ============================================================
# グローバル変数
# ============================================================

# APIリクエストのカウンター（デバッグ用）
request_counter = 0

# ============================================================
# Discord通知関連の関数
# ============================================================

def send_discord_notification(message, event_url=None):
    """
    Discord Webhookで通知を送信

    空き枠が見つかった際にDiscordに通知を送ります。
    DISCORD_WEBHOOK_URLが設定されていない場合は何もしません。

    Args:
        message: 送信するメッセージ本文
        event_url: イベントページのURL（オプション）
    """
    if not DISCORD_WEBHOOK_URL:
        # Webhook URLが設定されていない場合はスキップ
        return

    try:
        # Discord Embedフォーマットでメッセージを作成
        embed = {
            "title": "🎫 チケット空き枠検出！",
            "description": message,
            "color": 5814783,  # 緑色
            "timestamp": datetime.now().isoformat(),
            "footer": {
                "text": "Eventbrite チケット監視システム"
            }
        }

        # イベントURLがある場合は追加
        if event_url:
            embed["url"] = event_url

        payload = {
            "embeds": [embed]
        }

        # Webhookに送信
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)

        if response.status_code == 204:
            print("✓ Discord notification sent successfully")
        else:
            print(f"⚠ Discord notification failed: {response.status_code}")

    except Exception as e:
        print(f"⚠ Failed to send Discord notification: {e}")

# ============================================================
# APIリクエスト関連の関数
# ============================================================

def fetch_page(url, page):
    """
    指定されたページのチケット情報を取得

    Args:
        url: APIエンドポイントのURL
        page: 取得するページ番号

    Returns:
        dict: API応答のJSON（取得失敗時はNone）

    Raises:
        SystemExit: ステータスコード429（API制限）を検出した場合
    """
    global request_counter
    request_counter += 1
    print(f"[Request #{request_counter}] Fetching page {page}...")
    params = {'page': page}
    response = session.get(url, params=params)

    # API制限（Rate Limit）のチェック
    if response.status_code == 429:
        print("\n" + "=" * 60)
        print("⚠ API制限エラー（429 Too Many Requests）")
        print("=" * 60)
        print("Eventbrite APIのレート制限に達しました。")
        print("POLL_INTERVALの値を大きくしてください。")
        print(f"現在の設定: {POLL_INTERVAL}秒")
        print("推奨: 2.0秒以上")
        print("=" * 60)

        # Discord通知を送信
        error_message = f"⚠ API制限エラー（429）\n\nEventbrite APIのレート制限に達しました。\nPOLL_INTERVAL: {POLL_INTERVAL}秒\n\nプログラムを停止します。"
        send_discord_notification(error_message, None)

        # プログラムを終了
        exit(1)

    if response.status_code == 200:
        return response.json()

    return None

def check_ticket_availability():
    """
    イベントのチケット空き状況をチェック

    Eventbrite APIを使用して、指定されたイベントのチケットが
    購入可能かどうかを確認します。

    Returns:
        bool: チケットが利用可能な場合True、そうでない場合False
    """
    url = f'{BASE_URL}/events/{EVENT_ID}/ticket_classes/'
    try:
        all_ticket_classes = []
        all_responses = []
        available_tickets = []

        if ENABLE_PARALLEL_FETCH:
            # 完全並列取得: 1ページ目も並列で取得してページ数を把握
            first_page = fetch_page(url, 1)
            if not first_page:
                print(f"Error: Failed to fetch first page")
                return False

            all_responses.append(first_page)
            all_ticket_classes.extend(first_page.get('ticket_classes', []))

            # 早期終了: 1ページ目で在庫チェック
            if EARLY_EXIT:
                for tc in first_page.get('ticket_classes', []):
                    if tc.get('on_sale_status') == 'AVAILABLE':
                        available_tickets.append({
                            'id': tc['id'],
                            'name': tc['name'],
                            'on_sale_status': tc['on_sale_status']
                        })

                # 1ページ目で在庫発見したら即座に返す
                if available_tickets:
                    print(f"✓ TICKETS AVAILABLE! (Found in page 1)")
                    for ticket in available_tickets:
                        print(f"  → {ticket['name']}")
                    return True

            pagination = first_page.get('pagination', {})
            page_count = pagination.get('page_count', 1)

            # 2ページ目以降がある場合、すべてのページを同時に並列取得
            if page_count > 1:
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    future_to_page = {executor.submit(fetch_page, url, page): page
                                     for page in range(2, page_count + 1)}

                    for future in as_completed(future_to_page):
                        data = future.result()
                        if data:
                            all_responses.append(data)
                            tickets = data.get('ticket_classes', [])
                            all_ticket_classes.extend(tickets)

                            # 早期終了: 在庫を見つけたらすぐ返す
                            if EARLY_EXIT:
                                for tc in tickets:
                                    if tc.get('on_sale_status') == 'AVAILABLE':
                                        available_tickets.append({
                                            'id': tc['id'],
                                            'name': tc['name'],
                                            'on_sale_status': tc['on_sale_status']
                                        })

                                if available_tickets:
                                    print(f"✓ TICKETS AVAILABLE! (Found early)")
                                    for ticket in available_tickets:
                                        print(f"  → {ticket['name']}")
                                    return True
        else:
            # 順次取得（安全）
            page = 1
            while True:
                data = fetch_page(url, page)
                if not data:
                    break

                all_responses.append(data)
                tickets = data.get('ticket_classes', [])
                all_ticket_classes.extend(tickets)

                # 早期終了
                if EARLY_EXIT:
                    for tc in tickets:
                        if tc.get('on_sale_status') == 'AVAILABLE':
                            available_tickets.append({
                                'id': tc['id'],
                                'name': tc['name'],
                                'on_sale_status': tc['on_sale_status']
                            })

                    if available_tickets:
                        print(f"✓ TICKETS AVAILABLE!")
                        for ticket in available_tickets:
                            print(f"  → {ticket['name']}")
                        return True

                pagination = data.get('pagination', {})
                if not pagination.get('has_more_items', False):
                    break
                page += 1

        # JSON出力機能（ON/OFF切り替え可能）
        if SAVE_JSON_RESPONSE:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'response_{timestamp}.json'
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump({
                    'total_tickets': len(all_ticket_classes),
                    'pages': len(all_responses),
                    'responses': all_responses
                }, f, indent=2, ensure_ascii=False)

        # 早期終了が無効の場合、最後に全チケットをチェック
        if not EARLY_EXIT or not available_tickets:
            for tc in all_ticket_classes:
                on_sale_status = tc.get('on_sale_status', '')
                if on_sale_status == 'AVAILABLE':
                    available_tickets.append({
                        'id': tc['id'],
                        'name': tc['name'],
                        'on_sale_status': on_sale_status
                    })

            if available_tickets:
                print(f"✓ TICKETS AVAILABLE! ({len(available_tickets)}/{len(all_ticket_classes)})")
                for ticket in available_tickets:
                    print(f"  → {ticket['name']}")
                return True

        print(f"No tickets available. (Checked {len(all_ticket_classes)} tickets)")
        return False
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}")
        return False

# ============================================================
# Selenium自動操作関連の関数
# ============================================================

def automate_registration(driver):
    """
    チケット予約の自動化処理

    Seleniumを使用して以下の操作を自動で実行します：
    1. "Check availability"ボタンをクリック
    2. カレンダーから利用可能な日付を選択
    3. 時間スロットを選択
    4. "Register"ボタンをクリック

    iframe内の要素も自動で検出・操作します。
    エラー発生時はスクリーンショットを保存してデバッグを支援します。

    Args:
        driver: Selenium WebDriverインスタンス

    Returns:
        bool: 自動化が成功した場合True、失敗した場合False
    """
    try:
        # 要素の出現を最大20秒待機
        wait = WebDriverWait(driver, 20)

        # ================================================
        # Step 1: "Check availability"ボタンをクリック
        # ================================================
        print("Step 1: Clicking 'Check availability' button...")

        # 複数のセレクタを試行（ページ構造の変更に対応）
        check_availability_selectors = [
            "button[id*='check-availability']",
            "button.check-availability-btnbutton",
            "button:contains('Check availability')"
        ]

        check_availability_btn = None
        for selector in check_availability_selectors:
            try:
                # 各セレクタでボタンを探す
                check_availability_btn = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                print(f"  Found button with selector: {selector}")
                break
            except TimeoutException:
                # このセレクタでは見つからなかったので次を試す
                continue

        # CSSセレクタで見つからなかった場合、XPathを試す
        if not check_availability_btn:
            print("  Trying XPath method...")
            check_availability_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Check availability')]"))
            )

        # ボタンをクリック
        check_availability_btn.click()
        print("  ✓ Clicked Check availability button")

        # ================================================
        # iframe内の要素を操作するための準備
        # ================================================
        print("  Checking for iframe...")
        try:
            # モーダルがiframe内に表示される場合、iframeに切り替え
            iframe = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[id*='eventbrite-widget']"))
            )
            driver.switch_to.frame(iframe)
            print("  ✓ Switched to iframe")
        except TimeoutException:
            # iframeが存在しない場合はメインページで続行
            print("  No iframe found, continuing in main page")

        # ================================================
        # Step 2: 利用可能な日付を選択
        # ================================================
        print("Step 2: Selecting available date...")

        # Wait for calendar/modal to appear
        #print("  Waiting for calendar modal to appear...")
        #time.sleep(2)

        # The calendar dates are button elements
        # Look for buttons that are numeric (dates in calendar grid)
        available_date = None

        print("  Looking for calendar date buttons...")
        try:
            # First, let's wait a bit more for the content to load
            #time.sleep(2)

            # Debug: Save screenshot to see what's in the iframe
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            screenshot_path = f'iframe_screenshot_{timestamp}.png'
            driver.save_screenshot(screenshot_path)
            print(f"  Debug screenshot saved: {screenshot_path}")

            # Try to find all buttons in the iframe
            all_buttons = driver.find_elements(By.TAG_NAME, "button")
            print(f"  Found {len(all_buttons)} total buttons in iframe")

            # Show first few buttons for debugging
            if all_buttons:
                print("  Sample buttons found:")
                for i, btn in enumerate(all_buttons[:10]):
                    try:
                        btn_text = btn.text.strip() if btn.text else "[no text]"
                        btn_class = btn.get_attribute("class")
                        print(f"    [{i}] Text: '{btn_text}', Class: '{btn_class}'")
                    except Exception:
                        continue

            # Try to find date section with more flexible selector
            print("  Looking for Date section...")
            date_sections = driver.find_elements(By.XPATH, "//*[contains(text(), 'Date')]")
            print(f"  Found {len(date_sections)} elements containing 'Date'")

            # The calendar dates might not be buttons, try finding any clickable element with numeric text
            # Try multiple approaches:

            # 1. Try button elements
            date_buttons = driver.find_elements(By.XPATH,
                "//button[not(@disabled) and string-length(normalize-space(text())) <= 2 and number(text()) = number(text())]")
            print(f"  Found {len(date_buttons)} button date elements")

            # 2. Try any clickable element (div, span, etc.) with numeric text in calendar
            if len(date_buttons) == 0:
                print("  Trying non-button date elements...")
                date_elements = driver.find_elements(By.XPATH,
                    "//*[string-length(normalize-space(text())) <= 2 and number(text()) = number(text()) and not(contains(@class, 'disabled'))]")
                print(f"  Found {len(date_elements)} potential date elements")

                for elem in date_elements:
                    try:
                        if elem.is_displayed():
                            text = elem.text.strip()
                            tag = elem.tag_name
                            classes = elem.get_attribute("class")
                            print(f"    - Element: {tag}, text: '{text}', classes: '{classes}'")
                            if text.isdigit() and int(text) <= 31:
                                # If it's a p tag with dateText, we need to click the parent li
                                if tag == 'p' and 'dateText' in classes:
                                    parent = elem.find_element(By.XPATH, "./ancestor::li[1]")
                                    parent_classes = parent.get_attribute("class")
                                    print(f"  Found parent li with classes: '{parent_classes}'")

                                    # Check if the date is available (not unavailable or disabled)
                                    if 'unavailable' not in parent_classes.lower() and 'disabled' not in parent_classes.lower():
                                        available_date = parent
                                        print(f"  Selected available date: {text}")
                                        break
                                    else:
                                        print(f"  Date {text} is unavailable, trying next...")
                                        continue
                                else:
                                    available_date = elem
                                    print(f"  Selected date element: {text}")
                                    break
                    except Exception as e:
                        print(f"  Error checking element: {e}")
                        continue

            # 3. If still not found, try li elements (original structure)
            if not available_date:
                print("  Trying li elements...")
                li_elements = driver.find_elements(By.TAG_NAME, "li")
                print(f"  Found {len(li_elements)} li elements")

                for li in li_elements:
                    try:
                        if li.is_displayed() and 'enabled' in li.get_attribute('class').lower():
                            available_date = li
                            print(f"  Found enabled li element")
                            break
                    except Exception:
                        continue

        except Exception as e:
            print(f"  Error finding date buttons: {e}")
            import traceback
            traceback.print_exc()

        if not available_date:
            # Try alternative: look for any button in the calendar area
            print("  Trying alternative method...")
            try:
                # Look for buttons within a table or grid structure (common for calendars)
                date_buttons = driver.find_elements(By.CSS_SELECTOR, "table button, [role='grid'] button, [class*='calendar'] button")
                print(f"  Found {len(date_buttons)} buttons in calendar area")

                for btn in date_buttons:
                    try:
                        if btn.is_displayed() and btn.is_enabled() and btn.text.strip().isdigit():
                            available_date = btn
                            print(f"  Found date: {btn.text}")
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"  Alternative method failed: {e}")

        if not available_date:
            raise TimeoutException("Could not find available date button")

        available_date.click()
        print("  ✓ Selected available date")
        #time.sleep(3)

        # Step 3: Select time slot
        print("Step 3: Selecting time slot...")
        time_slot_selectors = [
            "div[role='button'][class*='TimeSlot']",
            "div.TimeSlot-moduleslot_1Z-Kw",
            "div[class*='timeSlotContainer']"
        ]

        time_slot = None
        for selector in time_slot_selectors:
            try:
                time_slot = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                print(f"  Found time slot with selector: {selector}")
                break
            except TimeoutException:
                continue

        if not time_slot:
            raise TimeoutException("Could not find time slot")

        time_slot.click()
        print("  ✓ Selected time slot")
        #time.sleep(3)

        # Step 4: Ticket quantity is already 1, skip
        print("Step 4: Ticket quantity already set to 1, skipping...")

        # Step 5: Click "Register" button
        print("Step 5: Clicking 'Register' button...")
        register_selectors = [
            "button[data-testid='eds-modal__primary-button']",
            "button[data-automation='eds-modalprimary-button']",
            "button.eds-btn--fill"
        ]

        register_btn = None
        for selector in register_selectors:
            try:
                register_btn = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                print(f"  Found register button with selector: {selector}")
                break
            except TimeoutException:
                continue

        if not register_btn:
            print("  Trying XPath method...")
            register_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Register')]"))
            )

        register_btn.click()
        print("  ✓ Clicked Register button")
        #time.sleep(2)

        print("\n✓ Registration automation completed successfully!")
        return True

    except TimeoutException as e:
        print(f"\n✗ Timeout error: Element not found within time limit")
        print(f"  Current URL: {driver.current_url}")
        print(f"  Page title: {driver.title}")
        print(f"  Error details: {e}")

        # Save screenshot for debugging
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            screenshot_path = f'error_screenshot_{timestamp}.png'
            driver.save_screenshot(screenshot_path)
            print(f"  Screenshot saved: {screenshot_path}")
        except Exception as screenshot_error:
            print(f"  Could not save screenshot: {screenshot_error}")

        return False
    except NoSuchElementException as e:
        print(f"\n✗ Element not found: {e}")
        return False
    except Exception as e:
        print(f"\n✗ Unexpected error during automation: {e}")
        import traceback
        traceback.print_exc()
        return False

# ============================================================
# ブラウザ操作関連の関数
# ============================================================

def open_browser_and_login():
    """
    ブラウザを開いてユーザーにログインしてもらう

    Chromeブラウザを起動し、Eventbriteのホームページを表示します。
    ユーザーが手動でログインした後、Enterキーを押すとCookieを保存して
    監視を開始します。ブラウザは開いたままにして、後の自動操作で再利用します。

    Returns:
        WebDriver: Seleniumのブラウザドライバーインスタンス
    """
    print("=" * 60)
    print("STEP 1: Login to Eventbrite")
    print("=" * 60)
    print("Opening browser for login...\n")

    try:
        # Chromeブラウザのオプション設定
        options = webdriver.ChromeOptions()
        options.add_argument("--no-first-run")  # 初回起動メッセージを表示しない
        options.add_argument("--no-default-browser-check")  # デフォルトブラウザチェックをスキップ

        # Chromeブラウザを起動
        driver = webdriver.Chrome(options=options)
        driver.maximize_window()  # ウィンドウを最大化

        # Eventbriteのホームページを開く
        print("Opening Eventbrite...")
        driver.get("https://www.eventbrite.com")

        # ユーザーにログインを促す
        print("\n" + "=" * 60)
        print("Please log in to your Eventbrite account in the browser.")
        print("After logging in, return here and press Enter to start monitoring.")
        print("=" * 60)

        # ユーザーがログインするまで待機
        input("\nPress Enter after you've logged in...")

        # ログイン完了
        print("\n✓ Login complete!")
        print("Starting ticket monitoring...\n")

        # ブラウザを開いたまま返す
        return driver

    except Exception as e:
        print(f"Error during login: {e}")
        import traceback
        traceback.print_exc()
        exit(1)

def purchase_attempt(driver):
    """
    既存のブラウザを使用してチケット予約を自動化

    ログイン済みのブラウザを使用して、チケット購入ページに移動し、
    以下の操作を自動で実行します：
    1. Check availabilityボタンをクリック
    2. 利用可能な日付を選択
    3. 時間スロットを選択
    4. Registerボタンをクリック

    最後のチェックアウトはユーザーが手動で完了します。

    Args:
        driver: ログイン済みのSelenium WebDriverインスタンス
    """
    print("\n🎫 Starting automation...")

    try:
        # 既存のブラウザでチケット購入ページに移動
        print(f"Navigating to: {CHECKOUT_BASE_URL}")
        driver.get(CHECKOUT_BASE_URL)

        # ページの読み込みを待機
        print("Waiting for page to load...")
        time.sleep(3)

        print(f"Page loaded. Title: {driver.title}")

        # 自動予約処理を実行
        success = automate_registration(driver)

        if success:
            # 自動化成功時
            print("\n✓ Automation completed successfully!")
            print(f"Current URL: {driver.current_url}")
            print("\n🎉 Browser is ready! Please complete the checkout manually.")
            input("\nPress Enter when you're done (browser will close)...")
        else:
            # 自動化失敗時
            print("\n⚠ Automation failed.")
            print("Please complete the process manually in the browser.")
            input("\nPress Enter when you're done (browser will close)...")

    except Exception as e:
        print(f"Error during automation: {e}")
        import traceback
        traceback.print_exc()
        print("\nPlease complete the process manually in the browser.")
        input("\nPress Enter when you're done (browser will close)...")

# ============================================================
# メイン処理
# ============================================================

def main():
    """
    プログラムのメイン処理

    処理の流れ：
    1. ブラウザを開いてユーザーにログインしてもらう
    2. ログイン完了後、チケットの空き状況を定期的に監視
    3. 空き枠が見つかったら、同じブラウザで自動予約を実行
    4. 最後のチェックアウトをユーザーに任せる
    """
    # ステップ1: ブラウザを開いてログイン
    driver = open_browser_and_login()

    # ステップ2: チケット監視を開始
    print("=" * 60)
    print("STEP 2: Ticket Monitoring")
    print("=" * 60)
    print(f"Monitoring Event ID: {EVENT_ID} (every {POLL_INTERVAL}s)")
    print("Press Ctrl+C to stop monitoring\n")
    print("Note: Browser will stay open. Do not close it!\n")

    attempts = 0
    try:
        # 無限ループでチケット状況を監視
        while True:
            attempts += 1
            print(f"[{attempts}] Checking...", end=" ")

            # チケットが利用可能かチェック
            if check_ticket_availability():
                # Discord通知を送信
                notification_message = f"チケットの空き枠が見つかりました！\n\nイベントID: {EVENT_ID}\n監視試行回数: {attempts}回"
                send_discord_notification(notification_message, CHECKOUT_BASE_URL)

                # 空き枠が見つかったら、既存のブラウザで自動予約を実行
                purchase_attempt(driver)
                break  # 予約処理が完了したらループを抜ける

            # 次のチェックまで待機
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        # Ctrl+Cで停止された場合
        print("\n\n⚠ Monitoring stopped by user")
        print(f"Total attempts: {attempts}")
        print("Exiting safely...")

    finally:
        # プログラム終了時にブラウザを閉じる
        if driver:
            driver.quit()
            print("Browser closed.")

if __name__ == "__main__":
    main()