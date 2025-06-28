import sys
import os
import time
import random
import json
import pandas as pd
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QFileDialog, QSpinBox, QGroupBox,
    QProgressBar, QLineEdit, QCheckBox, QMessageBox, QRadioButton
)
# --- Import QIcon ---
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver import ActionChains, Keys
import undetected_chromedriver as uc

# --- NEW: Import ctypes for Windows Taskbar Icon ---
if sys.platform == 'win32':
    import ctypes

# ==================================================================================================
# --- Worker Thread for Browser Automation ---
# ==================================================================================================

class Worker(QObject):
    """
    Handles all browser automation in a separate thread to keep the GUI responsive.
    Features AI comment generation, language detection, and human-like searching.
    """
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()
    update_progress_bar_signal = pyqtSignal(int)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.is_running = True
        self.driver = None
        self.posted_comments = []
        self.comment_personas = [
            "ask an insightful question about the content",
            "praise a specific detail (like editing, sound design, or a specific point made)",
            "share a personal emotional reaction to the video",
            "make a humorous but relevant observation",
            "compare a point in the video to something else",
            "express appreciation for the creator's effort and consistency"
        ]

    def _validate_api_key(self):
        """Performs a quick check to see if the API key is valid."""
        try:
            self.progress_signal.emit("Validating Gemini API Key...")
            genai.configure(api_key=self.config['api_key'])
            # A simple, low-cost call to check authentication
            models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            if not models:
                raise Exception("No valid models found for the API key.")
            self.progress_signal.emit("Gemini AI configured successfully.")
            return True
        except Exception as e:
            self.progress_signal.emit(f"FATAL: The provided Gemini API Key is invalid. Please check your key and restart the bot. Error: {e}")
            return False

    def run(self):
        """
        Main automation logic. Iterates through accounts and videos to post comments.
        """
        if self.config['comment_mode'] != 'manual':
            if not self._validate_api_key():
                self.finished_signal.emit()
                return

        total_steps = len(self.config['accounts']) * len(self.config['video_queries'])
        current_step = 0

        for index, account in self.config['accounts'].iterrows():
            if not self.is_running:
                break

            email = account['email']
            password = account['password']
            self.progress_signal.emit(f"--- Starting new session for account: {email} ---")

            try:
                self._initialize_driver()
                self._login_to_google(email, password)

                for query_data in self.config['video_queries']:
                    if not self.is_running:
                        break
                    
                    current_step += 1
                    progress_percentage = int((current_step / total_steps) * 100) if total_steps > 0 else 0
                    self.update_progress_bar_signal.emit(progress_percentage)

                    self._post_comment_on_video(query_data)
                    
                    sleep_time = random.uniform(self.config['min_delay'], self.config['max_delay'])
                    self.progress_signal.emit(f"Waiting for {sleep_time:.1f} seconds before next action...")
                    time.sleep(sleep_time)

            except Exception as e:
                self.progress_signal.emit(f"ERROR in session for {email}: {e}. Moving to next account.")
            finally:
                self._quit_driver()
                self.progress_signal.emit(f"Session closed for account {email}.")
        
        self.update_progress_bar_signal.emit(100)
        self.progress_signal.emit("--- All tasks completed! ---")
        self.finished_signal.emit()

    def _initialize_driver(self):
        self.progress_signal.emit("Initializing Chrome driver...")
        options = uc.ChromeOptions()
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        options.add_argument('--disable-infobars')
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        
        # --- FIX: Force the driver version to match the user's browser version ---
        self.progress_signal.emit("Forcing ChromeDriver version to 137 to match browser...")
        self.driver = uc.Chrome(options=options, use_suppress_welcome=True, version_main=137)
        self.progress_signal.emit("Chrome driver started.")

    def _login_to_google(self, email, password):
        self.progress_signal.emit("Attempting to log into Google...")
        self.driver.get("https://accounts.google.com/signin")

        WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[type="email"]'))
        ).send_keys(email)
        self.driver.find_element(By.XPATH, '//*[@id="identifierNext"]/div/button').click()
        time.sleep(random.uniform(2, 4))

        WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[type="password"]'))
        ).send_keys(password)
        self.driver.find_element(By.XPATH, '//*[@id="passwordNext"]/div/button').click()
        
        try:
            self.progress_signal.emit("Checking for 'Create a passkey' prompt...")
            not_now_button = WebDriverWait(self.driver, 7).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[contains(text(), 'Not now')]]"))
            )
            self.progress_signal.emit("Found 'Create a passkey' prompt. Clicking 'Not now'...")
            self.driver.execute_script("arguments[0].click();", not_now_button)
            time.sleep(random.uniform(2, 4))
        except TimeoutException:
            self.progress_signal.emit("No 'Create a passkey' prompt detected. Proceeding normally.")
            pass

        self.progress_signal.emit("Password submitted. Verifying login status...")
        
        try:
            self.driver.get("https://www.youtube.com")
            WebDriverWait(self.driver, 60).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "button#avatar-btn"))
            )
            self.progress_signal.emit(f"SUCCESS: Logged in and verified on YouTube as {email}.")
            self._handle_youtube_consent()
        except TimeoutException:
            page_text = self.driver.find_element(By.TAG_NAME, 'body').text
            if "Verify it's you" in page_text or "2-Step Verification" in page_text:
                self.progress_signal.emit(f"ACTION REQUIRED: Manual intervention needed for {email}. Please complete in the browser. Waiting for 60 seconds...")
                try:
                    WebDriverWait(self.driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "button#avatar-btn")))
                    self.progress_signal.emit(f"SUCCESS: Manual verification completed for {email}.")
                    self._handle_youtube_consent()
                except TimeoutException:
                    raise Exception("Login failed. Manual verification was not completed in time.")
            else:
                raise Exception("Login failed. Could not verify login on YouTube.")

    def _handle_youtube_consent(self):
        self.progress_signal.emit("Checking for cookie consent pop-up...")
        consent_button_xpaths = [
            '//ytd-button-renderer[.//yt-formatted-string[contains(text(),"Accept all")]]',
            '//button[.//span[contains(text(),"Accept all")]]'
        ]
        for xpath in consent_button_xpaths:
            try:
                consent_button = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                self.driver.execute_script("arguments[0].click();", consent_button)
                self.progress_signal.emit("Accepted cookies/consent.")
                time.sleep(random.uniform(2, 4))
                return
            except Exception:
                continue
        self.progress_signal.emit("No cookie consent pop-up found, proceeding.")
            
    def _get_video_details(self):
        """Fetches title, description, and transcript for language detection."""
        details = {'title': '', 'description': '', 'transcript': ''}
        # Use a more resilient way to fetch details, ignoring individual failures.
        try:
            details['title'] = self.driver.title.replace("- YouTube", "").strip()
        except Exception: pass
            
        try:
            expand_button = WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "tp-yt-paper-button#expand")))
            expand_button.click()
            time.sleep(0.5)
        except Exception:
            pass 

        try:
            description_element = WebDriverWait(self.driver, 2).until(EC.presence_of_element_located((By.CSS_SELECTOR, "ytd-text-inline-expander.ytd-video-secondary-info-renderer")))
            details['description'] = description_element.text[:1000]
        except Exception: pass

        try:
            video_id = self.driver.current_url.split('v=')[-1].split('&')[0]
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
            details['transcript'] = " ".join([d['text'] for d in transcript_list])[:2000]
        except Exception: pass

        if not any(details.values()):
             self.progress_signal.emit(f"Warning: Could not fetch any video details for language analysis.")
        
        return details

    def _detect_video_language_and_region(self):
        """Uses AI to detect the language and region of the video."""
        self.progress_signal.emit("Analyzing video for language and region...")
        details = self._get_video_details()
        
        if not (details['title'] or details['description'] or details['transcript']):
            self.progress_signal.emit("Not enough data to analyze video language. Defaulting to English.")
            return "English", "USA"

        combined_text = f"Title: {details['title']}. Description: {details['description']}. Transcript: {details['transcript']}"
        
        prompt = f"""
        Analyze the following text from a YouTube video and determine its primary spoken language and the likely country of origin for its target audience.
        Respond ONLY with a JSON object in the format: {{"language": "e.g., English, Urdu, Hindi", "region": "e.g., USA, Pakistan, India"}}
        
        Text to analyze: "{combined_text}"
        """
        try:
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            # Clean the response to make sure it's valid JSON
            clean_response = response.text.strip().replace('`', '')
            if clean_response.startswith('json'):
                clean_response = clean_response[4:]

            result = json.loads(clean_response)
            lang = result.get("language", "English")
            reg = result.get("region", "USA")
            self.progress_signal.emit(f"Detected Language: {lang}, Region: {reg}")
            return lang, reg
        except Exception as e:
            self.progress_signal.emit(f"AI language detection failed: {e}. Defaulting to English.")
            return "English", "USA"

    def _get_ai_comment(self, language, region):
        history_prompt = ""
        if self.posted_comments:
            recent_comments = self.posted_comments[-5:]
            history_examples = "\n".join([f"- \"{c}\"" for c in recent_comments])
            history_prompt = f"""CRITICAL: To ensure variety, DO NOT generate a comment that is similar in meaning, topic, or phrasing to any of these recently used comments. Also, avoid repeating common religious or cultural phrases (like 'Mashallah') if they have appeared in the recent comments below:\n{history_examples}"""

        language_instruction = ""
        if language == "Urdu" and region == "Pakistan":
            language_instruction = "IMPORTANT: The comment MUST be written in Roman Urdu (Urdu written with English letters)."

        length_categories = {
            "very short (3-7 words)": (3, 7),
            "medium (8-15 words)": (8, 15),
            "longer (16-30 words)": (16, 30)
        }
        selected_length_category = random.choice(list(length_categories.keys()))

        use_emoji = random.random() < 0.35
        if use_emoji:
            emoji_instruction = "IMPORTANT: You MUST add a single, relevant emoji at the end of the comment."
        else:
            emoji_instruction = "IMPORTANT: You MUST NOT use any emojis in this comment."

        prompt_rules = f"""
        **Rules:**
        1. Adhere to your assigned style.
        2. **Comment Length:** Your comment MUST be {selected_length_category}.
        3. **Emoji Use:** {emoji_instruction}
        4. No generic praise. Be specific.
        5. No hashtags.
        6. Critically check the history. The comment MUST be completely different.
        {language_instruction}
        """

        if self.config['comment_mode'] == 'persona':
            selected_persona = random.choice(self.comment_personas)
            self.progress_signal.emit(f"Generating comment with persona: '{selected_persona}'...")
            prompt = f"""You are an expert YouTube commenter. Your goal is to write a unique, human-like comment.
            **Your assigned commenting style for this specific comment is: "{selected_persona}".**
            {prompt_rules}
            {history_prompt}
            **Video Content for Analysis:** "{self.driver.title}"
            Now, generate the new, unique comment."""
        elif self.config['comment_mode'] == 'targeted':
            target_keyword = self.config['target_keyword']
            self.progress_signal.emit(f"Generating comment targeted at: '{target_keyword}'...")
            prompt = f"""You are an expert YouTube commenter. Your goal is to write a unique comment that naturally incorporates a specific theme.
            **Your assigned task is: Create a comment that subtly includes the theme of '{target_keyword}'.**
            {prompt_rules}
            {history_prompt}
            **Video Content for Analysis:** "{self.driver.title}"
            Now, generate the new, unique, targeted comment."""
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                model = genai.GenerativeModel('gemini-1.5-flash')
                generation_config = genai.types.GenerationConfig(temperature=1.0)
                response = model.generate_content(prompt, generation_config=generation_config)
                generated_comment = response.text.strip().replace('"', '')

                if generated_comment and generated_comment not in self.posted_comments:
                    self.posted_comments.append(generated_comment)
                    return generated_comment
                else:
                    self.progress_signal.emit(f"Duplicate or empty comment generated on attempt {attempt + 1}. Retrying...")
            except Exception as e:
                raise Exception(f"Gemini API call failed. Check your API Key and network connection. Error: {e}")

        raise Exception("Failed to generate a unique comment after several attempts.")

    def _search_and_play_video(self, query, partial_link):
        """Searches for a video and clicks the correct result using an optional partial link."""
        self.progress_signal.emit(f"Searching for video: '{query}'...")
        self.driver.get("https://www.youtube.com")
        time.sleep(2)
        
        search_box = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.NAME, "search_query")))
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        
        try:
            if partial_link:
                self.progress_signal.emit(f"Looking for video with link containing: '{partial_link}'")
                video_links = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, "ytd-video-renderer a#video-title"))
                )
                
                found_video = False
                for link_element in video_links:
                    href = link_element.get_attribute('href')
                    if href and partial_link in href:
                        self.progress_signal.emit(f"Found matching video: '{link_element.text}'. Clicking...")
                        link_element.click()
                        found_video = True
                        break
                
                if not found_video:
                    self.progress_signal.emit("Could not find video with matching link. Clicking first result as fallback.")
                    video_links[0].click()
            else:
                self.progress_signal.emit("No partial link provided. Clicking first video result.")
                first_video = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "ytd-video-renderer a#video-title")))
                first_video.click()

            time.sleep(random.uniform(3, 5)) 
        except Exception as e:
            raise Exception(f"Failed to find or click video in search results: {e}")
        
    def _post_comment_on_video(self, query_data):
        try:
            query, partial_link = query_data['query'], query_data['link']
            self._search_and_play_video(query, partial_link)
            
            try:
                disabled_message = self.driver.find_element(By.XPATH, "//*[@id='message' and contains(text(), 'Comments are turned off')]")
                if disabled_message:
                    self.progress_signal.emit(f"INFO: Comments are turned off for video '{query}'. Skipping.")
                    return 
            except NoSuchElementException:
                self.progress_signal.emit("Comments are enabled. Proceeding...")
                pass

            language, region = "English", "USA"
            if self.config.get('detect_language', False):
                language, region = self._detect_video_language_and_region()
            
            if self.config['comment_mode'] != 'manual':
                comment_to_post = self._get_ai_comment(language, region)
            else:
                comment_to_post = random.choice(self.config['comments'])

            self.progress_signal.emit(f"Preparing to post comment: '{comment_to_post[:50]}...'")
            
            try:
                self.progress_signal.emit("Scrolling to comments section...")
                comments_section = WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.ID, "comments")))
                self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", comments_section)
                time.sleep(random.uniform(1, 2))
                self.driver.execute_script("window.scrollBy(0, -200);")
                time.sleep(random.uniform(1, 2))
            except TimeoutException:
                raise Exception("Could not find the comments section (ID: #comments). The page may not have loaded correctly or has an unusual layout.")

            try:
                comment_box_placeholder = WebDriverWait(self.driver, 20).until(EC.element_to_be_clickable((By.ID, "placeholder-area")))
                self.progress_signal.emit("Activating comment box...")
                comment_box_placeholder.click()
            except TimeoutException:
                raise Exception("Could not find or click the comment box placeholder (ID: #placeholder-area).")

            try:
                comment_input_area = WebDriverWait(self.driver, 20).until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'div#contenteditable-root')))
                
                self.progress_signal.emit("Typing comment using robust method...")
                # 1. Use JS to set the content (handles emojis)
                escaped_comment = json.dumps(comment_to_post)
                self.driver.execute_script(f"arguments[0].textContent = {escaped_comment};", comment_input_area)
                
                # 2. Send a key press to trigger YouTube's internal state update
                comment_input_area.send_keys(" ")
                comment_input_area.send_keys(Keys.BACKSPACE)

            except TimeoutException:
                raise Exception("Could not find the comment input area after clicking the placeholder.")

            time.sleep(random.uniform(1, 2))

            try:
                self.progress_signal.emit("Waiting for submit button to be active...")
                submit_button = WebDriverWait(self.driver, 15).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "#submit-button:not([disabled])"))
                )
                self.progress_signal.emit("Submitting comment...")
                self.driver.execute_script("arguments[0].click();", submit_button)
            except TimeoutException:
                 raise Exception("The 'Comment' submit button did not become active after typing.")
            
            time.sleep(2)
            self.progress_signal.emit(f"SUCCESS: Commented on video found with query '{query}'.")

        except Exception as e:
            self.progress_signal.emit(f"FAILED: An error occurred while commenting on query '{query_data['query']}': {e}")

    def stop(self):
        self.is_running = False
        self.progress_signal.emit("--- Stop signal received. Finishing current task... ---")

    def _quit_driver(self):
        if self.driver:
            try:
                self.driver.quit()
                self.driver = None
            except Exception as e:
                self.progress_signal.emit(f"Error while quitting driver: {e}")

# ==================================================================================================
# --- Main GUI Window ---
# ==================================================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Next Level YouTube Commenter")
        self.setGeometry(100, 100, 850, 800)
        # --- NEW: Set the window icon ---
        # Ensure you have a 'logo.png' file in the same directory as the script.
        icon_path = None
        if os.path.exists("logo.ico"):
            icon_path = "logo.ico"
        elif os.path.exists("logo.png"):
            icon_path = "logo.png"

        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        input_group = QGroupBox("1. Input Data")
        input_layout = QVBoxLayout(input_group)
        self.layout.addWidget(input_group)

        input_layout.addWidget(QLabel("Video Search (Format: Search Query;Optional_Partial_Link)"))
        self.video_queries_input = QTextEdit()
        self.video_queries_input.setPlaceholderText("e.g., Afridi Farm Qurbani;watch?v=NEG9lafbzvI\ne.g., Latest Tech News")
        input_layout.addWidget(self.video_queries_input)
        
        accounts_layout = QHBoxLayout()
        self.load_accounts_btn = QPushButton("Load Accounts (.csv/.txt)")
        self.load_accounts_btn.clicked.connect(self.load_accounts_file)
        self.accounts_file_label = QLabel("No file loaded.")
        accounts_layout.addWidget(self.load_accounts_btn)
        accounts_layout.addWidget(self.accounts_file_label)
        input_layout.addLayout(accounts_layout)

        comment_group = QGroupBox("2. Comment Generation Strategy")
        comment_layout = QVBoxLayout(comment_group)
        self.layout.addWidget(comment_group)

        self.persona_ai_radio = QRadioButton("AI Generated (Varied & Creative)")
        self.persona_ai_radio.setChecked(True)
        self.persona_ai_radio.toggled.connect(self.toggle_comment_method)
        comment_layout.addWidget(self.persona_ai_radio)

        self.targeted_ai_radio = QRadioButton("Targeted AI Comments")
        self.targeted_ai_radio.toggled.connect(self.toggle_comment_method)
        comment_layout.addWidget(self.targeted_ai_radio)

        self.manual_radio = QRadioButton("Manual Comments from List")
        self.manual_radio.toggled.connect(self.toggle_comment_method)
        comment_layout.addWidget(self.manual_radio)

        self.language_detect_checkbox = QCheckBox("Auto-detect Language & Generate Regional Comments")
        self.language_detect_checkbox.setToolTip("When checked, AI will try to detect the video's language (e.g., Urdu) and use it for commenting (e.g., Roman Urdu).")
        self.language_detect_checkbox.setStyleSheet("margin-left: 20px;")
        comment_layout.addWidget(self.language_detect_checkbox)

        self.ai_settings_widget = QWidget()
        ai_layout = QHBoxLayout(self.ai_settings_widget)
        ai_layout.setContentsMargins(20, 5, 0, 0) 
        ai_layout.addWidget(QLabel("Gemini API Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Enter your Gemini API key")
        ai_layout.addWidget(self.api_key_input)
        comment_layout.addWidget(self.ai_settings_widget)
        
        self.targeted_keyword_widget = QWidget()
        targeted_layout = QHBoxLayout(self.targeted_keyword_widget)
        targeted_layout.setContentsMargins(20, 5, 0, 0)
        targeted_layout.addWidget(QLabel("Target Keyword/Theme:"))
        self.targeted_keyword_input = QLineEdit()
        self.targeted_keyword_input.setPlaceholderText("e.g., sound design, cinematography")
        targeted_layout.addWidget(self.targeted_keyword_input)
        comment_layout.addWidget(self.targeted_keyword_widget)

        self.manual_comments_widget = QWidget()
        manual_layout = QVBoxLayout(self.manual_comments_widget)
        manual_layout.setContentsMargins(20, 5, 0, 0)
        manual_layout.addWidget(QLabel("Manual Comments (one per line):"))
        self.comments_input = QTextEdit()
        self.comments_input.setPlaceholderText("Great video!\nAwesome content!")
        manual_layout.addWidget(self.comments_input)
        comment_layout.addWidget(self.manual_comments_widget)
        
        self.toggle_comment_method()

        config_group = QGroupBox("3. Configuration")
        config_layout = QHBoxLayout(config_group)
        self.layout.addWidget(config_group)

        config_layout.addWidget(QLabel("Delay (seconds): Min:"))
        self.min_delay_input = QSpinBox()
        self.min_delay_input.setValue(15)
        self.min_delay_input.setRange(5, 300)
        config_layout.addWidget(self.min_delay_input)

        config_layout.addWidget(QLabel("Max:"))
        self.max_delay_input = QSpinBox()
        self.max_delay_input.setValue(45)
        self.max_delay_input.setRange(10, 600)
        config_layout.addWidget(self.max_delay_input)
        
        control_group = QGroupBox("4. Control & Logging")
        control_layout = QVBoxLayout(control_group)
        self.layout.addWidget(control_group)

        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Bot")
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 8px;")
        self.start_btn.clicked.connect(self.start_bot)
        
        self.stop_btn = QPushButton("Stop Bot")
        self.stop_btn.setStyleSheet("background-color: #f44336; color: white; font-weight: bold; padding: 8px;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_bot)
        
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        control_layout.addLayout(button_layout)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        control_layout.addWidget(self.progress_bar)

        control_layout.addWidget(QLabel("Status Log:"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("background-color: #2B2B2B; color: #F0F0F0; border: 1px solid #444;")
        control_layout.addWidget(self.log_output)
        
        self.accounts_df = None
        self.worker_thread = None
        self.worker = None

    def toggle_comment_method(self):
        is_persona_ai = self.persona_ai_radio.isChecked()
        is_targeted_ai = self.targeted_ai_radio.isChecked()
        is_manual = self.manual_radio.isChecked()
        is_ai_mode = is_persona_ai or is_targeted_ai

        self.ai_settings_widget.setVisible(is_ai_mode)
        self.language_detect_checkbox.setVisible(is_ai_mode)
        self.targeted_keyword_widget.setVisible(is_targeted_ai)
        self.manual_comments_widget.setVisible(is_manual)
        
    def load_accounts_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Accounts File", "", "CSV/Text Files (*.csv *.txt)")
        if file_path:
            try:
                self.accounts_df = pd.read_csv(file_path, header=None, names=['email', 'password'])
                if self.accounts_df.isnull().values.any() or self.accounts_df.shape[1] != 2:
                    self.show_error_message("File Error", "File must have two columns (email, password) and no empty cells.")
                    self.accounts_df = None
                    return
                self.accounts_file_label.setText(os.path.basename(file_path))
                self.log_output.append(f"Successfully loaded {len(self.accounts_df)} accounts.")
            except Exception as e:
                self.show_error_message("File Load Error", f"Could not parse the file.\nError: {e}")
                self.accounts_file_label.setText("Failed to load file.")
                self.accounts_df = None

    def start_bot(self):
        config = {}
        
        raw_queries = [q.strip() for q in self.video_queries_input.toPlainText().strip().split('\n') if q.strip()]
        config['video_queries'] = []
        for line in raw_queries:
            parts = line.split(';', 1)
            query = parts[0].strip()
            link = parts[1].strip() if len(parts) > 1 else ""
            config['video_queries'].append({"query": query, "link": link})

        config['accounts'] = self.accounts_df
        config['min_delay'] = self.min_delay_input.value()
        config['max_delay'] = self.max_delay_input.value()
        
        if self.persona_ai_radio.isChecked():
            config['comment_mode'] = 'persona'
        elif self.targeted_ai_radio.isChecked():
            config['comment_mode'] = 'targeted'
        else:
            config['comment_mode'] = 'manual'
        
        config['detect_language'] = self.language_detect_checkbox.isChecked()
        config['api_key'] = self.api_key_input.text().strip()
        config['target_keyword'] = self.targeted_keyword_input.text().strip()
        config['comments'] = [c for c in self.comments_input.toPlainText().strip().split('\n') if c.strip()]
        
        if config['accounts'] is None or config['accounts'].empty:
            self.show_error_message("Input Error", "No accounts loaded.")
            return
        if not config['video_queries']:
            self.show_error_message("Input Error", "No video search queries provided.")
            return
        if config['comment_mode'] != 'manual' and not config['api_key']:
             self.show_error_message("Input Error", "An AI mode is selected, but no Gemini API Key was provided.")
             return
        if config['comment_mode'] == 'targeted' and not config['target_keyword']:
             self.show_error_message("Input Error", "Targeted AI mode is selected, but no target keyword was provided.")
             return
        if config['comment_mode'] == 'manual' and not config['comments']:
            self.show_error_message("Input Error", "Manual mode is selected, but no comments were provided.")
            return
        if config['min_delay'] > config['max_delay']:
            self.show_error_message("Config Error", "Minimum delay cannot be greater than maximum delay.")
            return

        self.log_output.clear()
        self.progress_bar.setValue(0)

        self.worker = Worker(config)
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished_signal.connect(self.on_bot_finished)
        self.worker.progress_signal.connect(self.update_log)
        self.worker.update_progress_bar_signal.connect(self.update_progress_bar)

        self.worker_thread.start()
        
        self.set_controls_enabled(False)
        self.log_output.append("Bot started...")

    def on_bot_finished(self):
        self.log_output.append("Worker thread has finished.")
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
        self.worker_thread = None
        self.worker = None
        self.set_controls_enabled(True)

    def stop_bot(self):
        if self.worker:
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.stop_btn.setText("Stopping...")

    def set_controls_enabled(self, is_enabled):
        self.start_btn.setEnabled(is_enabled)
        self.stop_btn.setEnabled(not is_enabled)
        self.stop_btn.setText("Stop Bot")
        
        for group_box in self.central_widget.findChildren(QGroupBox):
            if "Control & Logging" not in group_box.title():
                 group_box.setEnabled(is_enabled)
                 
    def update_log(self, message):
        if "ERROR" in message or "FATAL" in message or "FAILED" in message:
            message = f"<font color='red'>{message}</font>"
        elif "SUCCESS" in message:
            message = f"<font color='green'>{message}</font>"
        elif "ACTION REQUIRED" in message:
            message = f"<font color='orange'>{message}</font>"
        self.log_output.append(message)
    
    def update_progress_bar(self, value):
        self.progress_bar.setValue(value)
        
    def show_error_message(self, title, message):
        msg_box = QMessageBox(self)
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText(message)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()
        
    def closeEvent(self, event):
        if self.worker and self.worker_thread and self.worker_thread.isRunning():
            self.stop_bot()
            self.worker_thread.quit()
            self.worker_thread.wait()
        event.accept()

# ==================================================================================================
# --- Application Entry Point ---
# ==================================================================================================

def main():
    """Main function to run the application."""
    app = QApplication(sys.argv)
    if sys.platform == 'win32':
        myappid = 'mycompany.youtubebot.mainapp.1' # A different ID from the launcher
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    try:
        import google.generativeai
        import youtube_transcript_api
        import selenium
        import undetected_chromedriver
        import pandas
    except ImportError as e:
        print(f"Missing required package: {e.name}. Please install it using pip.")
        print("Required packages: pyqt6 pandas undetected-chromedriver selenium google-generativeai youtube-transcript-api")
        sys.exit(1)
        
    main()
