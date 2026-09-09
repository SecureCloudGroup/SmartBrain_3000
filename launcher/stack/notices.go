package stack

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Notice is one Neural Interface notice from GET /api/ni/notices: a fired alert,
// a card gone broken, or a card that repaired itself. IDs are stable and ordered
// by recency, which is what makes highest-seen-id dedupe possible.
type Notice struct {
	ID   int64  `json:"id"`
	Kind string `json:"kind"` // "alert" | "broken" | "repaired"
	Body string `json:"body"`
	TS   string `json:"ts"`
}

// FetchNotices reads the newest NI notices from the local app. Bounded and
// forgiving like Handshake: any trouble at all — the app is down, the vault is
// locked (423), a garbled body — reads as "no news" and the caller skips this
// poll. The X-SB-Local header marks the request as originating on THIS machine;
// the backend refuses the endpoint to bridged-in remote devices without it.
func FetchNotices(ctx context.Context, port int) ([]Notice, bool) {
	url := fmt.Sprintf("http://127.0.0.1:%d/api/ni/notices", port)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, false
	}
	req.Header.Set("X-SB-Local", "1")
	client := http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false // 423 (locked), 5xx, anything unexpected: no news
	}
	var notices []Notice
	if json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&notices) != nil {
		return nil, false
	}
	return notices, true
}
