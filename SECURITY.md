# Security policy

PenPlot Local processes untrusted image, PDF, and text files through third-party parsing libraries. Keep dependencies current, use an isolated Python environment, and do not process sensitive files on a shared system.

The application exports motion files but does not send them to a printer. Review every generated G-code file, run the air frame, and validate the physical setup before drawing.

Please report security issues privately through GitHub's security advisory feature rather than a public issue.

