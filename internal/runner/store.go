package runner

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

type database struct {
	Version     int                    `json:"version"`
	Connections map[string]*Connection `json:"connections"`
	Jobs        map[string]*Job        `json:"jobs"`
}

type store struct {
	root string
	lock *os.File
}

func openStore(root string) (*store, database, error) {
	var db database
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, db, err
	}
	info, err := os.Lstat(root)
	if err != nil {
		return nil, db, err
	}
	if !info.IsDir() || info.Mode().Perm()&0o077 != 0 {
		return nil, db, errors.New("runner state directory must be a private directory (0700)")
	}
	lock, err := os.OpenFile(filepath.Join(root, "daemon.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, db, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, db, fmt.Errorf("runner state is already locked: %w", err)
	}
	s := &store{root: root, lock: lock}
	data, err := os.ReadFile(filepath.Join(root, "state.json"))
	if errors.Is(err, os.ErrNotExist) {
		db = database{Version: 1, Connections: map[string]*Connection{}, Jobs: map[string]*Job{}}
	} else if err != nil {
		s.close()
		return nil, db, err
	} else if err := json.Unmarshal(data, &db); err != nil || db.Version != 1 || db.Connections == nil || db.Jobs == nil {
		s.close()
		return nil, db, errors.New("invalid runner state; refusing to discard durable records")
	}
	for id, c := range db.Connections {
		if c == nil || !validID(id) || c.ID != id || !c.Owner.valid() || (c.State != Active && c.State != Closed && c.State != Lost) || c.CreatedAt.IsZero() || c.LastActivity.IsZero() {
			s.close()
			return nil, db, errors.New("invalid connection record")
		}
	}
	type dedupIdentity struct {
		owner Owner
		key   string
	}
	seen := map[dedupIdentity]bool{}
	for id, j := range db.Jobs {
		if j == nil || !validID(id) || j.ID != id || !j.Owner.valid() || j.IdempotencyKey == "" || db.Connections[j.ConnectionID] == nil || j.TimeoutMillis <= 0 || len(j.Command) > MaxCommandBytes || strings.ContainsRune(j.Command, 0) || j.AcceptedAt.IsZero() {
			s.close()
			return nil, db, errors.New("invalid job record")
		}
		c := db.Connections[j.ConnectionID]
		identity := dedupIdentity{j.Owner, j.IdempotencyKey}
		validState := false
		switch j.State {
		case Accepted, Running, Completed, Failed, Cancelled, Lost:
			validState = true
		}
		if !validState || c.Owner != j.Owner || seen[identity] || (terminal(j.State) && j.FinishedAt.IsZero()) || (!terminal(j.State) && c.BusyJob != id) {
			s.close()
			return nil, db, errors.New("inconsistent durable job record")
		}
		seen[identity] = true
	}
	return s, db, nil
}

func (s *store) close() error { return s.lock.Close() }

func (s *store) save(db database) error {
	data, err := json.Marshal(db)
	if err != nil {
		return err
	}
	return atomicFile(filepath.Join(s.root, "state.json"), data)
}

func atomicFile(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".state-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err := f.Chmod(0o600); err != nil {
		f.Close()
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func newID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(value[:])
}

func validID(value string) bool {
	if len(value) != 32 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
