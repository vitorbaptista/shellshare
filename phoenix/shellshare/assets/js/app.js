// Include phoenix_html to handle method=PUT/DELETE in forms and buttons.
import "phoenix_html"
// Establish Phoenix Socket and LiveView configuration.
import {Socket} from "phoenix"
import {LiveSocket} from "phoenix_live_view"
import topbar from "../vendor/topbar"
import {Terminal} from "../vendor/xterm"

// Terminal hook for xterm.js integration
let Hooks = {}

Hooks.Terminal = {
  term: null,
  
  mounted() {
    const container = this.el
    const cols = parseInt(container.dataset.cols) || 80
    const rows = parseInt(container.dataset.rows) || 24
    const buffer = container.dataset.buffer || ""
    
    // Initialize xterm.js terminal
    this.term = new Terminal({
      cols: cols,
      rows: rows,
      cursorBlink: false,
      disableStdin: true,
      theme: {
        background: '#000000',
        foreground: '#ffffff',
        cursor: '#ffffff'
      }
    })
    
    this.term.open(container)
    
    // Write initial buffer if any
    if (buffer) {
      try {
        const decoded = this.decodeMessage(buffer)
        if (decoded) {
          this.term.write(decoded)
        }
      } catch (e) {
        console.error("Failed to decode initial buffer:", e)
      }
    }
    
    // Handle terminal data events from LiveView
    this.handleEvent("terminal:data", ({message, size}) => {
      // Resize terminal if needed
      if (size && size.cols && size.rows) {
        if (this.term.cols !== size.cols || this.term.rows !== size.rows) {
          this.term.resize(size.cols, size.rows)
        }
      }
      
      // Write message
      if (message) {
        try {
          const decoded = this.decodeMessage(message)
          if (decoded) {
            this.term.write(decoded)
          }
        } catch (e) {
          console.error("Failed to decode message:", e)
        }
      }
    })
    
    this.handleEvent("terminal:closed", () => {
      this.term.write("\r\n\x1b[31m[Session ended]\x1b[0m\r\n")
    })
  },
  
  decodeMessage(base64Message) {
    // Decode base64 -> URL-decoded content
    // Same logic as the original JS client
    try {
      const urlEncoded = atob(base64Message)
      return decodeURIComponent(urlEncoded)
    } catch (e) {
      console.error("Decode error:", e)
      return null
    }
  },
  
  destroyed() {
    if (this.term) {
      this.term.dispose()
    }
  }
}

let csrfToken = document.querySelector("meta[name='csrf-token']").getAttribute("content")
let liveSocket = new LiveSocket("/live", Socket, {
  longPollFallbackMs: 2500,
  params: {_csrf_token: csrfToken},
  hooks: Hooks
})

// Show progress bar on live navigation and form submits
topbar.config({barColors: {0: "#29d"}, shadowColor: "rgba(0, 0, 0, .3)"})
window.addEventListener("phx:page-loading-start", _info => topbar.show(300))
window.addEventListener("phx:page-loading-stop", _info => topbar.hide())

// connect if there are any LiveViews on the page
liveSocket.connect()

// expose liveSocket on window for web console debug logs and latency simulation:
// >> liveSocket.enableDebug()
// >> liveSocket.enableLatencySim(1000)  // enabled for duration of browser session
// >> liveSocket.disableLatencySim()
window.liveSocket = liveSocket
