/**
 * Incremental Server-Sent Events parser (WHATWG rules).
 *
 * Why not EventSource: it only dispatches named events that have a listener (new
 * event kinds would vanish) and cannot send Last-Event-ID on the first connect.
 * The panel reads the stream with fetch and feeds the decoded text to this parser.
 */

export interface SseMessage {
  /** The `id:` field in effect for this message (kept across messages), or null. */
  id: string | null;
  /** The `event:` field, "message" when absent. */
  event: string;
  /** The `data:` lines joined with a newline. */
  data: string;
}

export class SseParser {
  private buffer = "";
  private lastId: string | null = null;
  private pendingCr = false;
  private data: string[] = [];
  private hasData = false;
  private eventName = "";

  /** Feed one decoded chunk; get back every message completed by it. */
  feed(chunk: string): SseMessage[] {
    const out: SseMessage[] = [];
    let text = chunk;
    if (this.pendingCr) {
      // The previous chunk ended in CR; a LF right after it is part of the same line break.
      this.pendingCr = false;
      if (text.startsWith("\n")) text = text.slice(1);
    }
    if (text.endsWith("\r")) this.pendingCr = true;
    this.buffer += text;
    // Normalise CRLF and lone CR to LF; a trailing CR was already counted as a break.
    const normalised = this.buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const lines = normalised.split("\n");
    this.buffer = lines.pop() ?? "";
    for (const line of lines) {
      const message = this.line(line);
      if (message) out.push(message);
    }
    return out;
  }

  /** The id of the last message seen, for the Last-Event-ID header on reconnect. */
  get lastEventId(): string | null {
    return this.lastId;
  }

  private line(line: string): SseMessage | null {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return null; // comment: the server's keep-alive ping
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "data":
        this.data.push(value);
        this.hasData = true;
        break;
      case "event":
        this.eventName = value;
        break;
      case "id":
        if (!value.includes("\0")) this.lastId = value;
        break;
      default:
        break; // retry and unknown fields are ignored
    }
    return null;
  }

  private dispatch(): SseMessage | null {
    const had = this.hasData;
    const message: SseMessage = {
      id: this.lastId,
      event: this.eventName === "" ? "message" : this.eventName,
      data: this.data.join("\n"),
    };
    this.data = [];
    this.hasData = false;
    this.eventName = "";
    return had ? message : null;
  }
}
