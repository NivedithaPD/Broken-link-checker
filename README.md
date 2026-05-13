The Broken Link Checker is a Python-based tool that scans any website and identifies broken (dead) links. It crawls through your pages, tests every hyperlink, and generates a report of links that return errors (404, 500, timeouts, etc.).
Prerequisites
Download & Install Python
Python is the programming language that powers the Broken Link Checker. You need it installed on your machine first.
👉 **Download Python here:** [https://www.python.org/downloads/ ](https://www.python.org/downloads/ )
Installation Tips
•	**Windows:** Run the installer and **check the box that says "Add Python to PATH"** before clicking Install. This is critical — without it, the `python` and `pip` commands won't work in your terminal.
•	**macOS:** Python 3 may already be installed. Open Terminal and type `python3 --version` to check. If not found, download it from the link above.
Verify Installation
Open your terminal (Command Prompt on Windows, Terminal on macOS/Linux) and run:
python --version
or
python3 --version
You should see something like `Python 3.x.x`. If you do, you are good to go.
 Download the Broken Link Checker
Download the **Broken Link Checker Python script** (`broken_link_checker.py`) available here or ask WriterBear to your Desktop or a dedicated project folder.
- -
NivedithaPD/Broken-link-checker
Install Required Dependencies
 The Broken Link Checker relies on three Python libraries. Open your terminal, navigate to the folder where you saved the script, and run this single command:
pip install flask requests beautifulsoup4
``Note:** On some systems, you may need to use `pip3` instead of `pip`:
pip3 install flask requests beautifulsoup4
What These Libraries Do
 
| Library            | Purpose                                                     |
| ------------------ | ----------------------------------------------------------- |
| **Flask**          | Provides a lightweight web interface to view your results   |
| **Requests**       | Handles making HTTP requests to test each link              |
| **BeautifulSoup4** | Parses the HTML of web pages to extract all hyperlinks      |
 
Verify Installation
Run the following to confirm everything installed correctly:
pip show flask requests beautifulsoup4
You should see version and location info for all three packages.
 Run the Broken Link Checker
Once Python and all dependencies are installed, run the checker with:
python broken_link_checker.py
or
python3 broken_link_checker.py
Or
Click broken_link_checker (Available in the folder location)
Follow the on-screen prompts to enter the URL you want to scan. The tool will crawl the site, test every link, and output a report of any broken links it finds.
Quick-Start Summary
1.	Download Python        →  https://www.python.org/downloads/ 
2.	Verify Python install  →  python --version
3.	Get the script         →  broken_link_checker.py
4.	Install dependencies   →  pip install flask requests beautifulsoup4
5.	Run the checker        →  python broken_link_checker.py
Troubleshooting
**`pip` is not recognized?**
Make sure Python was added to your system PATH during installation. On Windows, re-run the Python installer and check "Add Python to PATH."
**Permission errors on macOS/Linux?**
Try prefixing the install command with `sudo`:
```bash
sudo pip3 install flask requests beautifulsoup4
**Script won't start?**
Ensure you're in the correct directory. Use `cd` to navigate to the folder containing `broken_link_checker.py` before running it.

