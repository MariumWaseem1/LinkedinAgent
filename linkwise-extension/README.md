# Linkwise by REV3 - Chrome Extension

Analyse your LinkedIn profile and get a complete personal brand strategy powered by live market intelligence.

## How to install for testing

1. Open Chrome
2. Go to chrome://extensions
3. Enable Developer Mode (toggle top right)
4. Click Load unpacked
5. Select the linkwise-extension folder
6. Pin the Linkwise icon to your toolbar
7. Go to linkedin.com/in/your-profile
8. Click the Linkwise icon
9. Click Analyse My Profile
10. Your strategy opens at linkedinagent.rev3.uk

## How it works

1. The extension reads your LinkedIn profile page DOM
2. Scrolls the page to trigger lazy-loaded sections
3. Extracts name, headline, about, experience, education, skills, certifications
4. Sends the text to the REV3 backend for validation
5. Opens linkedinagent.rev3.uk and passes your profile data
6. REV3 analyses your profile with live market intelligence

## How to publish to Chrome Web Store

1. Zip the entire linkwise-extension folder
2. Go to chrome.google.com/webstore/devconsole
3. Pay the one-time $5 developer fee
4. Click New Item and upload the zip file
5. Fill in the store listing (name, description, screenshots)
6. Add the privacy policy URL (required)
7. Submit for review (takes 1 to 3 business days)

## Notes

- The extension only activates on linkedin.com/in/* pages
- No profile data is stored permanently
- Everything is processed in-session only
- Replace placeholder icons in icons/ with your branded PNG files before publishing
