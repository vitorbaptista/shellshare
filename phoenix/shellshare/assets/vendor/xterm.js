/**
 * Minimal xterm.js-compatible terminal for shellshare
 * 
 * This is a simplified terminal emulator for read-only viewing.
 * For production, consider using the full xterm.js library:
 * npm install xterm
 * 
 * @license MIT
 */

class Terminal {
  constructor(options = {}) {
    this.cols = options.cols || 80;
    this.rows = options.rows || 24;
    this.theme = options.theme || {
      background: '#000000',
      foreground: '#ffffff',
      cursor: '#ffffff'
    };
    this.element = null;
    this.pre = null;
    this.buffer = '';
  }

  open(container) {
    // Create terminal container
    this.element = document.createElement('div');
    this.element.className = 'xterm';
    this.element.style.cssText = `
      background: ${this.theme.background};
      color: ${this.theme.foreground};
      font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
      font-size: 14px;
      line-height: 1.2;
      padding: 8px;
      overflow: auto;
      white-space: pre;
      min-height: ${this.rows * 1.2 * 14}px;
    `;
    
    // Create viewport
    const viewport = document.createElement('div');
    viewport.className = 'xterm-viewport';
    viewport.style.cssText = `
      overflow-y: auto;
      height: ${this.rows * 1.2 * 14}px;
    `;
    
    // Create pre element for content
    this.pre = document.createElement('pre');
    this.pre.className = 'xterm-rows';
    this.pre.style.margin = '0';
    this.pre.style.whiteSpace = 'pre-wrap';
    this.pre.style.wordBreak = 'break-all';
    
    viewport.appendChild(this.pre);
    this.element.appendChild(viewport);
    container.innerHTML = '';
    container.appendChild(this.element);
  }

  write(data) {
    if (!this.pre) return;
    
    // Simple ANSI escape sequence handling
    const processed = this.processAnsi(data);
    this.buffer += processed;
    this.pre.innerHTML = this.buffer;
    
    // Auto-scroll to bottom
    const viewport = this.element.querySelector('.xterm-viewport');
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }

  processAnsi(str) {
    // Basic ANSI color code handling
    // This handles the most common escape sequences
    const ansiColorMap = {
      '30': 'color: #000000',
      '31': 'color: #cd0000',
      '32': 'color: #00cd00',
      '33': 'color: #cdcd00',
      '34': 'color: #0000ee',
      '35': 'color: #cd00cd',
      '36': 'color: #00cdcd',
      '37': 'color: #e5e5e5',
      '90': 'color: #7f7f7f',
      '91': 'color: #ff0000',
      '92': 'color: #00ff00',
      '93': 'color: #ffff00',
      '94': 'color: #5c5cff',
      '95': 'color: #ff00ff',
      '96': 'color: #00ffff',
      '97': 'color: #ffffff',
      '40': 'background-color: #000000',
      '41': 'background-color: #cd0000',
      '42': 'background-color: #00cd00',
      '43': 'background-color: #cdcd00',
      '44': 'background-color: #0000ee',
      '45': 'background-color: #cd00cd',
      '46': 'background-color: #00cdcd',
      '47': 'background-color: #e5e5e5',
      '1': 'font-weight: bold',
      '4': 'text-decoration: underline',
    };

    let result = '';
    let currentStyles = [];
    let i = 0;

    while (i < str.length) {
      // Check for ESC sequence
      if (str[i] === '\x1b' && str[i + 1] === '[') {
        let j = i + 2;
        let code = '';
        
        // Read until we hit a letter
        while (j < str.length && !/[A-Za-z]/.test(str[j])) {
          code += str[j];
          j++;
        }
        
        if (j < str.length) {
          const command = str[j];
          
          if (command === 'm') {
            // SGR (Select Graphic Rendition)
            const codes = code.split(';');
            
            for (const c of codes) {
              if (c === '0' || c === '') {
                // Reset
                if (currentStyles.length > 0) {
                  result += '</span>';
                  currentStyles = [];
                }
              } else if (ansiColorMap[c]) {
                if (currentStyles.length > 0) {
                  result += '</span>';
                }
                currentStyles = [ansiColorMap[c]];
                result += `<span style="${currentStyles.join(';')}">`;
              }
            }
          }
          // Other commands: K (erase line), H/f (cursor position), etc.
          // For read-only display, we can mostly ignore cursor movements
          
          i = j + 1;
          continue;
        }
      }
      
      // Handle special characters
      if (str[i] === '<') {
        result += '&lt;';
      } else if (str[i] === '>') {
        result += '&gt;';
      } else if (str[i] === '&') {
        result += '&amp;';
      } else if (str[i] === '\r') {
        // Carriage return - handled with \n
      } else {
        result += str[i];
      }
      
      i++;
    }
    
    // Close any open spans
    if (currentStyles.length > 0) {
      result += '</span>';
    }
    
    return result;
  }

  resize(cols, rows) {
    this.cols = cols;
    this.rows = rows;
    
    if (this.element) {
      const viewport = this.element.querySelector('.xterm-viewport');
      if (viewport) {
        viewport.style.height = `${this.rows * 1.2 * 14}px`;
      }
    }
  }

  clear() {
    this.buffer = '';
    if (this.pre) {
      this.pre.innerHTML = '';
    }
  }

  dispose() {
    if (this.element && this.element.parentNode) {
      this.element.parentNode.removeChild(this.element);
    }
    this.element = null;
    this.pre = null;
    this.buffer = '';
  }
}

export { Terminal };
