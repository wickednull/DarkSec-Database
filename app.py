import sys
import os
import re
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QGridLayout, QPushButton, QLabel, QScrollArea, QFrame, QSizePolicy
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, QTimer # Import QUrl, pyqtSignal, QTimer directly
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase # For opening PDFs externally and custom fonts
import subprocess # Import subprocess for xdg-open
import shutil # Import shutil for file copying
import tempfile # Import tempfile for creating temporary files

# Define the QSS for the cyberpunk/hacker theme
CYBERPUNK_QSS = """
QWidget {
    background-color: #0d1117; /* Deep dark background */
    color: #00ff00; /* Neon green text */
    font-family: "Hack", "Consolas", "Monaco", monospace; /* Monospace font */
    font-size: 14px;
    selection-background-color: #008800; /* Selection highlight */
    selection-color: #ffffff;
}

QLabel {
    color: #00ff00;
    padding: 5px;
}

QLabel#header_label { /* Specific styling for the header */
    color: #33ff33; /* Brighter neon green */
    font-size: 28px; /* Larger font */
    font-weight: bold;
    text-shadow: 0 0 8px #00ff00, 0 0 15px #00aa00; /* More intense glow */
    margin-bottom: 20px;
    border-bottom: 2px solid #00ff00; /* Underline effect */
    padding-bottom: 10px;
}

/* General button styling */
QPushButton {
    background-color: #1a202c; /* Darker button background */
    border: 2px solid #00ff00; /* Thicker neon green border */
    color: #00ff00; /* Neon green text */
    padding: 12px 20px; /* More padding */
    text-align: center; /* Center text for better button feel */
    margin: 7px 0; /* More margin */
    border-radius: 0px; /* Sharp edges */
    min-height: 35px; /* Taller buttons */
    font-size: 16px; /* Larger font for buttons */
    letter-spacing: 1px; /* Spaced out text */
    max-width: 400px; /* Limit button width */
}

QPushButton:hover {
    background-color: #2c343e; /* Lighter on hover */
    border: 2px solid #33ff33; /* Brighter neon green border on hover */
    color: #33ff33;
    /* No box-shadow, as it's not supported. Rely on border/color change. */
}

QPushButton:pressed {
    background-color: #00ff00; /* Solid neon green when pressed */
    color: #0d1117; /* Dark text when pressed */
    border: 2px solid #00ff00;
}

/* Specific styling for the back button */
QPushButton#back_button {
    max-width: 150px; /* Smaller width for back button */
    text-align: center;
    margin-top: 20px;
}

QScrollArea {
    border: none; /* Remove border from scroll area */
}

QScrollBar:vertical {
    border: 1px solid #008800;
    background: #1a202c;
    width: 12px; /* Wider scrollbar */
    margin: 0px 0px 0px 0px;
}
QScrollBar::handle:vertical {
    background: #00ff00;
    min-height: 30px; /* Taller handle */
    border-radius: 0px; /* Sharp edges */
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    background: none;
    height: 0px;
    width: 0px;
}
QScrollBar::add-page:vertical, QScrollBar:sub-page:vertical {
    background: none;
}
"""

class CategoryCard(QFrame):
    clicked = pyqtSignal(str) # Signal to emit when card is clicked

    def __init__(self, category_name, file_count, description, category_path, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.category_path = category_path
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding) # Make card expand

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(5)

        # Title (Category Name + File Count)
        title_label = QLabel(f"<b>[{category_name}]</b> {file_count} files available.")
        title_label.setStyleSheet("font-size: 1.1em; color: #33ff33;") # Match card b style
        layout.addWidget(title_label)

        # Description
        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("font-size: 0.95em; color: #00ff00;") # Match card .desc style
        layout.addWidget(desc_label)

        self.setLayout(layout)
        self.setStyleSheet(self.styleSheet() + """
            CategoryCard {
                border: 1px solid #00ff00;
                background: #1a202c;
            }
            CategoryCard:hover {
                background: #2c343e;
                border: 1px solid #33ff33;
            }
        """) # Apply card styling

    def mousePressEvent(self, event):
        self.clicked.emit(self.category_path)
        super().mousePressEvent(event)

class DarkSecSurvivalDatabase(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DarkSec Survival Database")
        self.setGeometry(100, 100, 800, 600)

        # Apply the QSS theme
        self.setStyleSheet(CYBERPUNK_QSS)

        # Attempt to load a custom font if available
        font_id = QFontDatabase.addApplicationFont(":/fonts/Hack-Regular.ttf") # Assuming font is bundled
        if font_id != -1:
            font_families = QFontDatabase.applicationFontFamilies(font_id)
            if font_families:
                self.setFont(QFont(font_families[0]))
        else:
            print("Custom font 'Hack-Regular.ttf' not found or failed to load. Using fallback fonts.")

        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)

        self.current_view = "main_categories"
        self.history = [] # To manage navigation history

        self.init_ui()
        self._load_main_categories()

    def init_ui(self):
        self.header_label = QLabel("<h1>// DarkSec Survival Database <small>— Preserving the code of humanity before the system wipes it clean.</small></h1>")
        self.header_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_layout.addWidget(self.header_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.main_layout.addWidget(self.scroll_area)

        self.back_button = QPushButton("← Back")
        self.back_button.clicked.connect(self._go_back)
        self.main_layout.addWidget(self.back_button)
        self.back_button.hide() # Hide initially

        # Determine the base path for resources, handling PyInstaller bundling
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            self.base_path = sys._MEIPASS
        else:
            self.base_path = os.path.dirname(__file__)

    def _clear_content_area(self):
        # Clear existing widgets from the scroll area
        if self.scroll_area.widget():
            self.scroll_area.widget().deleteLater()

    def _load_main_categories(self):
        self._clear_content_area()
        content_widget = QWidget()
        content_layout = QGridLayout(content_widget) # Use QGridLayout
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter) # Center the grid itself
        self.scroll_area.setWidget(content_widget)

        main_index_html_path = os.path.join(self.base_path, 'index.html')

        if os.path.exists(main_index_html_path):
            with open(main_index_html_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # Regex to find category cards in the main index.html
                # Example: <a class="card" href="chembio/index.html"><b>[CHEMBIO]</b> 27 files available.<span class="desc">...</span></a>
                matches = re.findall(r'<a class="card" href="(.*?)/index.html"><b>\[(.*?)\]</b> (\d+) files available\.<span class="desc">(.*?)</span></a>', content)
                
                if matches:
                    for href_dir, category_name, file_count, description in matches:
                        full_category_path = os.path.join(self.base_path, href_dir)
                        category_card = CategoryCard(category_name, file_count, description, full_category_path)
                        category_card.clicked.connect(lambda path=full_category_path: QTimer.singleShot(0, lambda: self._open_category(path)))
                        # Add to grid, dynamically calculating row and column
                        row = content_layout.count() // 2 # 2 columns per row
                        col = content_layout.count() % 2
                        content_layout.addWidget(category_card, row, col)
                
                    # Set column stretch to make columns expand equally
                    content_layout.setColumnStretch(0, 1)
                    content_layout.setColumnStretch(1, 1)
                else:
                    content_layout.addWidget(QLabel("No categories found in main index.html."))
        else:
            content_layout.addWidget(QLabel("Main index.html not found."))

    def _open_category(self, category_path):
        self._clear_content_area()
        self.current_view = "pdf_list"
        self.history.append(self.current_view) # Add to history
        self.back_button.show()

        content_widget = QWidget()
        content_layout = QGridLayout(content_widget) # Use QGridLayout
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter) # Center the grid itself
        self.scroll_area.setWidget(content_widget)

        category_index_html_path = os.path.join(category_path, 'index.html')

        if os.path.exists(category_index_html_path):
            with open(category_index_html_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # Regex to find PDF links in category index.html
                # Example: <a class="card" href="SOLIDS IN CONTACT WITH NATURAL WATERS.pdf"><b>SOLIDS IN CONTACT WITH NATURAL WATERS.pdf</b></a>
                matches = re.findall(r'<a class="card" href="(.*?)"><b>(.*?)</b></a>', content)

                if matches:
                    for href, file_name_html in matches:
                        file_name = file_name_html # Keep original for display
                        full_pdf_path = os.path.join(category_path, href)
                        
                        pdf_card_button = QPushButton(f"<b>{file_name}</b>")
                        pdf_card_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding) # Make button expand
                        pdf_card_button.setStyleSheet("""
                            QPushButton {
                                background-color: #1a202c;
                                border: 1px solid #00ff00;
                                color: #00ff00;
                                padding: 15px;
                                text-align: left;
                                border-radius: 0px;
                                font-size: 1.1em;
                            }
                            QPushButton:hover {
                                background-color: #2c343e;
                                border: 1px solid #33ff33;
                                color: #33ff33;
                            }
                        """)
                        pdf_card_button.clicked.connect(lambda checked, path=full_pdf_path: self._open_pdf(path))
                        # Add to grid, dynamically calculating row and column
                        row = content_layout.count() // 2 # 2 columns per row
                        col = content_layout.count() % 2
                        content_layout.addWidget(pdf_card_button, row, col)
                
                    # Set column stretch to make columns expand equally
                    content_layout.setColumnStretch(0, 1)
                    content_layout.setColumnStretch(1, 1)
                else:
                    content_layout.addWidget(QLabel(f"No PDFs found in {os.path.basename(category_path)}/index.html."))
        else:
            content_layout.addWidget(QLabel(f"Category index.html not found for {os.path.basename(category_path)}."))

    def _open_pdf(self, pdf_path):
        print(f"Attempting to open PDF: {pdf_path}")
        if os.path.exists(pdf_path):
            opened = False
            temp_pdf_dest = None
            try:
                # Create a temporary file path in the system's temp directory
                temp_dir = tempfile.gettempdir()
                # Use a generic, simple filename for the temporary copy
                temp_pdf_name_simple = "temp_pdf.pdf"
                
                # Ensure unique name if file already exists
                counter = 0
                temp_pdf_dest = os.path.join(temp_dir, temp_pdf_name_simple)
                while os.path.exists(temp_pdf_dest):
                    counter += 1
                    temp_pdf_dest = os.path.join(temp_dir, f"temp_pdf_{counter}.pdf")

                shutil.copy2(pdf_path, temp_pdf_dest)
                os.chmod(temp_pdf_dest, 0o755) # Make it readable and executable
                print(f"Copied PDF to temporary location: {temp_pdf_dest} with permissions 755")

                # Now open the copied temporary file
                # --- Attempt 1: xdg-open ---
                if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
                    try:
                        subprocess.check_call(['xdg-open', temp_pdf_dest])
                        opened = True
                        print(f"Successfully opened PDF using xdg-open: {temp_pdf_dest}")
                    except FileNotFoundError:
                        print("Error: 'xdg-open' command not found. Trying specific viewers.")
                    except subprocess.CalledProcessError as e:
                        print(f"Error: xdg-open failed with exit code {e.returncode}. Stderr: {e.stderr}. Trying specific viewers.")
                    except Exception as e:
                        print(f"Error launching xdg-open: {e}. Trying specific viewers.")
                
                # --- Attempt 2: Specific PDF viewers (Linux only) ---
                if not opened and getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
                    viewers = ['evince', 'okular', 'zathura', 'atril', 'qpdfview', 'xpdf']
                    for viewer in viewers:
                        try:
                            subprocess.check_call([viewer, temp_pdf_dest])
                            opened = True
                            print(f"Successfully opened PDF using {viewer}: {temp_pdf_dest}")
                            break
                        except FileNotFoundError:
                            print(f"Viewer '{viewer}' not found. Trying next.")
                        except Exception as e:
                            print(f"Error launching {viewer}: {e}. Trying next.")

                # --- Attempt 3: QDesktopServices (Fallback for all platforms/failures) ---
                if not opened:
                    print(f"Attempting to open PDF with QDesktopServices: {temp_pdf_dest}")
                    opened = QDesktopServices.openUrl(QUrl.fromLocalFile(temp_pdf_dest))
                    if opened:
                        print(f"Successfully opened PDF using QDesktopServices: {temp_pdf_dest}")
                    else:
                        print(f"Error: QDesktopServices could not open PDF {temp_pdf_dest}. No application associated or permission denied.")
                
            except Exception as e:
                print(f"Error copying or opening temporary PDF: {e}")
            finally:
                # Note: For a robust solution, you might want to schedule deletion of temp_pdf_dest
                # after the external viewer is closed or when the app exits. For now, we leave it.
                pass

        else:
            print(f"Error: PDF file not found at {pdf_path}")

    def _go_back(self):
        if self.history:
            self.history.pop() # Remove current view from history
            if not self.history: # If history is empty, go to main categories
                self._load_main_categories()
            else: # Go back to the previous view (e.g., a category list)
                # This part needs more sophisticated history management if there are multiple levels
                # For now, we'll just assume going back from a PDF list goes to main categories
                self._load_main_categories()
        else:
            self._load_main_categories()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DarkSecSurvivalDatabase()
    window.show()
    sys.exit(app.exec())
