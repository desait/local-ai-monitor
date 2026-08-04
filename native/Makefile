# Native always-on plane (C feed only by default).
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11
OUTDIR = dist
SENSOR = $(OUTDIR)/local-ai-monitor-sensor
APPSCAN = $(OUTDIR)/local-ai-monitor-appscan
DAEMON = $(OUTDIR)/local-ai-monitord
LITEBAR = $(OUTDIR)/local-ai-monitor-litebar

.PHONY: all clean install rss parity n2 n3 litebar

all: $(SENSOR) $(APPSCAN) $(DAEMON)

$(OUTDIR):
	mkdir -p $(OUTDIR)

$(SENSOR): sensor.c | $(OUTDIR)
	$(CC) $(CFLAGS) -o $(SENSOR) sensor.c

$(APPSCAN): appscan.c | $(OUTDIR)
	$(CC) $(CFLAGS) -o $(APPSCAN) appscan.c

$(DAEMON): localaimonitord.c | $(OUTDIR)
	$(CC) $(CFLAGS) -o $(DAEMON) localaimonitord.c

# Native fallback menu bar target. The default product app is SwiftUI under menubar/.
$(LITEBAR): litebar/main.m | $(OUTDIR)
	$(CC) -O2 -fobjc-arc -framework Cocoa -o $(LITEBAR) litebar/main.m

litebar: $(LITEBAR)

clean:
	rm -f $(SENSOR) $(APPSCAN) $(DAEMON) $(LITEBAR)

install: all
	mkdir -p $(HOME)/.local/bin
	cp $(SENSOR) $(HOME)/.local/bin/local-ai-monitor-sensor
	cp $(APPSCAN) $(HOME)/.local/bin/local-ai-monitor-appscan
	cp $(DAEMON) $(HOME)/.local/bin/local-ai-monitord
	chmod +x $(HOME)/.local/bin/local-ai-monitor-sensor $(HOME)/.local/bin/local-ai-monitor-appscan \
		$(HOME)/.local/bin/local-ai-monitord
	@echo "installed: local-ai-monitord C feed only; SwiftUI menubar is built from menubar/"

rss: all
	@echo "=== local-ai-monitord ==="
	@/usr/bin/time -l $(DAEMON) -q --state /tmp/local-ai-monitord-rss 2>&1 | awk '/maximum resident set size/ {printf "max_rss_bytes %s  (~%.1f MB)\n", $$1, $$1/1024/1024}'
	@echo "=== local-ai-monitor-litebar (launch 2s) ==="
	@$(LITEBAR) & LP=$$!; sleep 2; ps -o rss= -p $$LP | awk '{printf "litebar_rss_mb %.2f\n", $$1/1024}'; kill $$LP 2>/dev/null; wait $$LP 2>/dev/null

parity: $(APPSCAN)
	@python3 ../scripts/n1_parity.py $(APPSCAN)

n2: $(DAEMON)
	@python3 ../scripts/n2_schema.py $(DAEMON)

n3: $(DAEMON)
	@python3 ../scripts/n3_softstop.py $(DAEMON)
