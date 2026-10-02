"""The Python files that make up the AnyCam add-on, entry point first.

The release gate, the tests and the packager read this list. The gate
also checks it against the imports and against the Dockerfile.
"""
MODULES = ["camera_discovery.py", "camera_db.py", "page_script.py",
           "anycam_brand.py", "anycam_go2rtc.py", "anycam_host.py",
           "anycam_motion.py", "anycam_page.py", "anycam_probe.py",
           "anycam_scan.py", "anycam_storage.py"]
