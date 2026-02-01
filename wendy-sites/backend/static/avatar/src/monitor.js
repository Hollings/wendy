/**
 * monitor.js - Chat-focused monitor renderer
 *
 * Base layer: Chat messages from Discord
 * Overlays: Terminal output, diffs, etc. (rendered on top)
 *
 * Typing is character-by-character, synced with keyboard presses.
 */

// =============================================================================
// Configuration
// =============================================================================

const COLORS = {
    background: '#1a1a1a',
    text: '#e0e0e0',
    textDim: '#888888',
    textMuted: '#555555',
    author: '#60a5fa',
    authorWendy: '#f472b6',
    cursor: '#f472b6',
    // Diff colors
    diffAdd: '#4ade80',      // Green for additions
    diffRemove: '#f87171',   // Red for removals
    diffAddBg: '#16291a',    // Dark green background
    diffRemoveBg: '#2d1619', // Dark red background
    filePath: '#fbbf24',     // Yellow for file path
};

const LAYOUT = {
    padding: 20,
    lineHeight: 28,
    messageGap: 16,
    fontSize: 22,
    authorFontSize: 18,
};

// =============================================================================
// Monitor Class
// =============================================================================

export class Monitor {
    constructor(canvas, texture) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.texture = texture;
        this.width = canvas.width;
        this.height = canvas.height;

        // Chat state
        this.messages = [];
        this.maxMessages = 20;

        // Wendy's typing state (null when not typing)
        this.typing = null;  // { author, fullText, visibleText }

        // Overlay state (null = show chat, otherwise show overlay)
        // Types: 'diff' | 'file'
        this.overlay = null;

        // Cursor blink
        this.cursorVisible = true;
        this.cursorBlinkTime = 0;

        // Initial render
        this.render();
    }

    // =========================================================================
    // Public API
    // =========================================================================

    /**
     * Set the message list (typically from check_messages response)
     * @param {Array<{author: string, content: string}>} messages
     */
    setMessages(messages) {
        this.messages = messages.slice(-this.maxMessages);
        this.render();
    }

    /**
     * Add a single message to the chat
     * @param {string} author
     * @param {string} content
     */
    addMessage(author, content) {
        this.messages.push({ author, content });
        if (this.messages.length > this.maxMessages) {
            this.messages.shift();
        }
        this.render();
    }

    /**
     * Start Wendy typing a message
     * @param {string} fullText - The complete message she will type
     */
    startTyping(fullText) {
        this.typing = {
            author: 'Wendy',
            fullText,
            visibleText: '',
        };
        this.render();
    }

    /**
     * Add a character to Wendy's typing (call when key is pressed)
     * @param {string} char - Character to add
     */
    typeChar(char) {
        if (!this.typing) return;

        this.typing.visibleText += char;
        this.cursorVisible = true;  // Reset cursor on keypress
        this.cursorBlinkTime = 0;
        this.render();
    }

    /**
     * Finish typing - add message to chat and clear typing state
     */
    finishTyping() {
        if (!this.typing) return;

        this.addMessage(this.typing.author, this.typing.fullText);
        this.typing = null;
        this.render();
    }

    /**
     * Check if Wendy is currently typing
     * @returns {boolean}
     */
    isTyping() {
        return this.typing !== null;
    }

    /**
     * Check if typing is complete (all characters typed)
     * @returns {boolean}
     */
    isTypingComplete() {
        if (!this.typing) return false;
        return this.typing.visibleText.length >= this.typing.fullText.length;
    }

    // =========================================================================
    // Diff Overlay
    // =========================================================================

    /**
     * Show a diff overlay
     * @param {string} filePath - File being edited
     * @param {string} oldString - Original text
     * @param {string} newString - New text
     */
    showDiff(filePath, oldString, newString) {
        // Split into lines for display
        const oldLines = (oldString || '').split('\n');
        const newLines = (newString || '').split('\n');

        this.overlay = {
            type: 'diff',
            filePath,
            oldLines,
            newLines,
        };
        this.render();
    }

    /**
     * Show a file content overlay
     * @param {string} filePath - File being read
     * @param {string} content - File content (null = loading)
     */
    showFile(filePath, content = null) {
        this.overlay = {
            type: 'file',
            filePath,
            content,
            lines: content ? content.split('\n') : null,
        };
        this.render();
    }

    /**
     * Show a terminal command overlay
     * @param {string} command - Command being executed
     */
    showTerminal(command) {
        this.overlay = {
            type: 'terminal',
            command,
            output: null,
        };
        this.render();
    }

    /**
     * Update terminal output (when tool_result arrives)
     * @param {string} output - Command output
     */
    updateTerminalOutput(output) {
        if (this.overlay?.type === 'terminal') {
            this.overlay.output = output;
            this.render();
        }
    }

    /**
     * Update file content (when tool_result arrives)
     * @param {string} content - File content
     */
    updateFileContent(content) {
        if (this.overlay?.type === 'file') {
            this.overlay.content = content;
            this.overlay.lines = content ? content.split('\n') : [];
            this.render();
        }
    }

    /**
     * Clear any overlay and return to chat view
     */
    clearOverlay() {
        this.overlay = null;
        this.render();
    }

    /**
     * Check if an overlay is active
     * @returns {boolean}
     */
    hasOverlay() {
        return this.overlay !== null;
    }

    /**
     * Update animations (call from render loop)
     * @param {number} delta - Time since last frame in seconds
     */
    update(delta) {
        // Cursor blink
        this.cursorBlinkTime += delta;
        if (this.cursorBlinkTime >= 0.5) {
            this.cursorBlinkTime = 0;
            this.cursorVisible = !this.cursorVisible;
            if (this.typing) {
                this.render();
            }
        }
    }

    // =========================================================================
    // Rendering
    // =========================================================================

    /**
     * Main render function - draws chat and any overlays
     */
    render() {
        this.clear();

        // Draw overlay if active, otherwise draw chat
        if (this.overlay) {
            this.drawOverlay();
        } else {
            this.drawChat();
        }

        this.markDirty();
    }

    clear() {
        this.ctx.fillStyle = COLORS.background;
        this.ctx.fillRect(0, 0, this.width, this.height);
    }

    markDirty() {
        this.texture.needsUpdate = true;
    }

    /**
     * Draw the chat messages from bottom to top
     */
    drawChat() {
        const { padding, lineHeight, messageGap, fontSize, authorFontSize } = LAYOUT;
        const maxWidth = this.width - padding * 2;

        this.ctx.textBaseline = 'top';

        // Build list of messages to render (including typing)
        const renderMessages = [...this.messages];

        // Add typing message at the end if active
        if (this.typing) {
            renderMessages.push({
                author: this.typing.author,
                content: this.typing.visibleText,
                isTyping: true,
            });
        }

        // Calculate layout for each message (author line + wrapped content)
        const layouts = renderMessages.map(msg => {
            this.ctx.font = `${fontSize}px monospace`;
            const lines = this.wrapText(msg.content || ' ', maxWidth);
            const height = authorFontSize + 4 + (lines.length * lineHeight);
            return { msg, lines, height };
        });

        // Draw from bottom up
        let y = this.height - padding;

        for (let i = layouts.length - 1; i >= 0; i--) {
            const { msg, lines, height } = layouts[i];

            // Check if we have room
            if (y - height < padding) break;

            y -= height;

            // Author name
            this.ctx.font = `${authorFontSize}px monospace`;
            this.ctx.fillStyle = msg.author === 'Wendy' ? COLORS.authorWendy : COLORS.author;
            this.ctx.fillText(msg.author, padding, y);

            // Message content
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.text;

            let lineY = y + authorFontSize + 4;
            for (let j = 0; j < lines.length; j++) {
                this.ctx.fillText(lines[j], padding, lineY);

                // Draw cursor at end of last line if typing
                if (msg.isTyping && j === lines.length - 1 && this.cursorVisible) {
                    const cursorX = padding + this.ctx.measureText(lines[j]).width + 2;
                    this.ctx.fillStyle = COLORS.cursor;
                    this.ctx.fillRect(cursorX, lineY, 3, lineHeight - 4);
                    this.ctx.fillStyle = COLORS.text;
                }

                lineY += lineHeight;
            }

            y -= messageGap;
        }

        // Draw "empty" state if no messages
        if (renderMessages.length === 0) {
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.textAlign = 'center';
            this.ctx.fillText('No messages', this.width / 2, this.height / 2);
            this.ctx.textAlign = 'left';
        }
    }

    /**
     * Draw the current overlay
     */
    drawOverlay() {
        if (!this.overlay) return;

        if (this.overlay.type === 'diff') {
            this.drawDiffOverlay();
        } else if (this.overlay.type === 'file') {
            this.drawFileOverlay();
        } else if (this.overlay.type === 'terminal') {
            this.drawTerminalOverlay();
        }
    }

    /**
     * Draw the diff overlay
     */
    drawDiffOverlay() {
        if (!this.overlay || this.overlay.type !== 'diff') return;

        const { padding, lineHeight, fontSize } = LAYOUT;
        const { filePath, oldLines, newLines } = this.overlay;

        this.ctx.textBaseline = 'top';

        // Draw file path header
        let y = padding;
        this.ctx.font = `bold ${fontSize}px monospace`;
        this.ctx.fillStyle = COLORS.filePath;

        // Truncate file path if too long
        const displayPath = filePath.length > 40
            ? '...' + filePath.slice(-37)
            : filePath;
        this.ctx.fillText(displayPath, padding, y);
        y += lineHeight + 8;

        // Draw separator line
        this.ctx.strokeStyle = COLORS.textMuted;
        this.ctx.beginPath();
        this.ctx.moveTo(padding, y);
        this.ctx.lineTo(this.width - padding, y);
        this.ctx.stroke();
        y += 12;

        // Calculate how many lines we can fit
        const availableHeight = this.height - y - padding;
        const maxLines = Math.floor(availableHeight / lineHeight);

        // Smaller font for code
        const codeFontSize = 16;
        this.ctx.font = `${codeFontSize}px monospace`;

        // Draw removed lines (red)
        const maxOldLines = Math.min(oldLines.length, Math.floor(maxLines / 2));
        for (let i = 0; i < maxOldLines; i++) {
            // Background
            this.ctx.fillStyle = COLORS.diffRemoveBg;
            this.ctx.fillRect(padding - 4, y - 2, this.width - padding * 2 + 8, lineHeight - 4);

            // Text
            this.ctx.fillStyle = COLORS.diffRemove;
            const line = '- ' + this.truncateLine(oldLines[i], this.width - padding * 2 - 20);
            this.ctx.fillText(line, padding, y);
            y += lineHeight;
        }

        // Show "..." if truncated
        if (oldLines.length > maxOldLines) {
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.fillText(`  ... ${oldLines.length - maxOldLines} more lines`, padding, y);
            y += lineHeight;
        }

        // Small gap between old and new
        y += 8;

        // Draw added lines (green)
        const remainingLines = Math.floor((this.height - y - padding) / lineHeight);
        const maxNewLines = Math.min(newLines.length, remainingLines);
        for (let i = 0; i < maxNewLines; i++) {
            // Background
            this.ctx.fillStyle = COLORS.diffAddBg;
            this.ctx.fillRect(padding - 4, y - 2, this.width - padding * 2 + 8, lineHeight - 4);

            // Text
            this.ctx.fillStyle = COLORS.diffAdd;
            const line = '+ ' + this.truncateLine(newLines[i], this.width - padding * 2 - 20);
            this.ctx.fillText(line, padding, y);
            y += lineHeight;
        }

        // Show "..." if truncated
        if (newLines.length > maxNewLines) {
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.fillText(`  ... ${newLines.length - maxNewLines} more lines`, padding, y);
        }
    }

    /**
     * Draw the file content overlay
     */
    drawFileOverlay() {
        if (!this.overlay || this.overlay.type !== 'file') return;

        const { padding, lineHeight, fontSize } = LAYOUT;
        const { filePath, lines } = this.overlay;

        this.ctx.textBaseline = 'top';

        // Draw file path header
        let y = padding;
        this.ctx.font = `bold ${fontSize}px monospace`;
        this.ctx.fillStyle = COLORS.filePath;

        // Truncate file path if too long
        const displayPath = filePath.length > 40
            ? '...' + filePath.slice(-37)
            : filePath;
        this.ctx.fillText(displayPath, padding, y);
        y += lineHeight + 8;

        // Draw separator line
        this.ctx.strokeStyle = COLORS.textMuted;
        this.ctx.beginPath();
        this.ctx.moveTo(padding, y);
        this.ctx.lineTo(this.width - padding, y);
        this.ctx.stroke();
        y += 12;

        // Show loading state if no content yet
        if (!lines) {
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.fillText('Loading...', padding, y);
            return;
        }

        // Calculate how many lines we can fit
        const availableHeight = this.height - y - padding;
        const codeFontSize = 16;
        const codeLineHeight = lineHeight - 4;
        const maxLines = Math.floor(availableHeight / codeLineHeight);

        // Smaller font for code
        this.ctx.font = `${codeFontSize}px monospace`;

        // Draw file content
        const linesToShow = Math.min(lines.length, maxLines);
        for (let i = 0; i < linesToShow; i++) {
            // Line number
            this.ctx.fillStyle = COLORS.textMuted;
            const lineNum = String(i + 1).padStart(3, ' ');
            this.ctx.fillText(lineNum, padding, y);

            // Line content
            this.ctx.fillStyle = COLORS.text;
            const lineContent = this.truncateLine(lines[i], this.width - padding * 2 - 40);
            this.ctx.fillText(lineContent, padding + 40, y);

            y += codeLineHeight;
        }

        // Show "..." if truncated
        if (lines.length > maxLines) {
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.fillText(`  ... ${lines.length - maxLines} more lines`, padding, y);
        }
    }

    /**
     * Draw the terminal command overlay
     */
    drawTerminalOverlay() {
        if (!this.overlay || this.overlay.type !== 'terminal') return;

        const { padding, lineHeight, fontSize } = LAYOUT;
        const { command, output } = this.overlay;

        this.ctx.textBaseline = 'top';

        let y = padding;

        // Draw prompt
        this.ctx.font = `bold ${fontSize}px monospace`;
        this.ctx.fillStyle = COLORS.diffAdd;  // Green for prompt
        this.ctx.fillText('$', padding, y);

        // Draw command
        this.ctx.font = `${fontSize}px monospace`;
        this.ctx.fillStyle = COLORS.text;
        const cmdDisplay = this.truncateLine(command || '', this.width - padding * 2 - 30);
        this.ctx.fillText(cmdDisplay, padding + 25, y);
        y += lineHeight + 12;

        // Draw separator
        this.ctx.strokeStyle = COLORS.textMuted;
        this.ctx.beginPath();
        this.ctx.moveTo(padding, y);
        this.ctx.lineTo(this.width - padding, y);
        this.ctx.stroke();
        y += 12;

        // Draw output if available
        if (output) {
            const codeFontSize = 16;
            const codeLineHeight = lineHeight - 4;
            this.ctx.font = `${codeFontSize}px monospace`;
            this.ctx.fillStyle = COLORS.textDim;

            const outputLines = output.split('\n');
            const availableHeight = this.height - y - padding;
            const maxLines = Math.floor(availableHeight / codeLineHeight);
            const linesToShow = Math.min(outputLines.length, maxLines);

            for (let i = 0; i < linesToShow; i++) {
                const line = this.truncateLine(outputLines[i], this.width - padding * 2);
                this.ctx.fillText(line, padding, y);
                y += codeLineHeight;
            }

            if (outputLines.length > maxLines) {
                this.ctx.fillStyle = COLORS.textMuted;
                this.ctx.fillText(`... ${outputLines.length - maxLines} more lines`, padding, y);
            }
        } else {
            // Show running indicator
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.fillText('Running...', padding, y);
        }
    }

    /**
     * Truncate a line to fit within maxWidth
     * @param {string} line
     * @param {number} maxWidth
     * @returns {string}
     */
    truncateLine(line, maxWidth) {
        if (!line) return '';

        // Quick check - if line is short, return as-is
        if (this.ctx.measureText(line).width <= maxWidth) {
            return line;
        }

        // Binary search for truncation point
        let lo = 0, hi = line.length;
        while (lo < hi) {
            const mid = Math.floor((lo + hi + 1) / 2);
            if (this.ctx.measureText(line.slice(0, mid) + '...').width <= maxWidth) {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }

        return line.slice(0, lo) + '...';
    }

    /**
     * Wrap text to fit within maxWidth
     * @param {string} text
     * @param {number} maxWidth
     * @returns {string[]}
     */
    wrapText(text, maxWidth) {
        if (!text) return [''];

        const words = text.split(' ');
        const lines = [];
        let currentLine = '';

        for (const word of words) {
            const testLine = currentLine ? `${currentLine} ${word}` : word;
            const width = this.ctx.measureText(testLine).width;

            if (width > maxWidth && currentLine) {
                lines.push(currentLine);
                currentLine = word;
            } else {
                currentLine = testLine;
            }
        }

        if (currentLine) {
            lines.push(currentLine);
        }

        return lines.length ? lines : [''];
    }
}
