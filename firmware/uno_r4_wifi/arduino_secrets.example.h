// Copy this file to "arduino_secrets.h" (same folder) and fill in your values.
// arduino_secrets.h is ignored by git so your passwords are never published.

#define SECRET_WIFI_SSID   "YourHomeWiFi"
#define SECRET_WIFI_PASS   "your-wifi-password"

// Where the website runs, without https:// — e.g. "my-heating.onrender.com"
// or your computer's local IP address like "192.168.1.50" while testing.
#define SECRET_SERVER_HOST "my-heating.onrender.com"
#define SECRET_SERVER_PORT 443      // 443 for https, 8000 for a local test server
#define SECRET_USE_TLS     1        // 1 for https, 0 for plain http on your home network

// The "Device key" shown on the website's Devices page.
#define SECRET_DEVICE_KEY  "paste-device-key-here"
