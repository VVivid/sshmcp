package runner

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"time"
)

type supervisor interface {
	Start(context.Context, string) error
	Stop(context.Context, string) error
}

type systemdSupervisor struct{ config Config }

func unit(id string) string { return "sshmcp-shell-" + id + ".service" }

func (s systemdSupervisor) Start(ctx context.Context, id string) error {
	c := s.config
	command := exec.CommandContext(ctx, "systemd-run", "--user", "--quiet", "--collect", "--unit="+unit(id),
		"--property=KillMode=control-group", "--property=TimeoutStopSec=10s", "--property=SendSIGKILL=yes",
		c.Executable, "shell-worker", "--root", c.Root+"/connections/"+id,
		"--socket", workerSocket(c, id), "--home", c.Home,
		"--cancel-grace", c.CancelGrace.String(), "--output-limit", strconv.FormatInt(c.OutputLimit, 10))
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("start shell unit: %w: %s", err, output)
	}
	return nil
}

func (s systemdSupervisor) Stop(ctx context.Context, id string) error {
	// An absent transient unit is already stopped. Checking first avoids treating
	// a previous boot's collected units as a startup recovery failure.
	out, err := exec.CommandContext(ctx, "systemctl", "--user", "show", unit(id), "--property=LoadState", "--value").Output()
	if err != nil {
		return fmt.Errorf("inspect shell unit: %w", err)
	}
	if string(out) == "not-found\n" {
		return nil
	}
	if output, err := exec.CommandContext(ctx, "systemctl", "--user", "stop", unit(id)).CombinedOutput(); err != nil {
		return fmt.Errorf("stop shell unit: %w: %s", err, output)
	}
	return nil
}

func stopShell(s supervisor, id string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	return s.Stop(ctx, id)
}
