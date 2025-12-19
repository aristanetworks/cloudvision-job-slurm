# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
#
# Makefile for CloudVision Slurm Integration

# Disable built-in implicit rules
MAKEFLAGS += --no-builtin-rules
.SUFFIXES:

# ============================================================================
# Configuration Variables
# ============================================================================

# CloudVision API Configuration (REQUIRED)
API_SERVER ?=
API_TOKEN ?=

# Installation Paths
SLURM_CONF_DIR ?= /etc/slurm
INSTALL_DIR ?= /opt/slurm/cloudvision
LOG_DIR ?= /var/log/slurm


# Python Configuration
PYTHON ?= python3
PIP ?= pip3

# ============================================================================
# Internal Variables
# ============================================================================
JOB_HOOK_SCRIPT := cv-job-hook.py
NODE_INVENTORY_SCRIPT := cv-node-inventory.py
INTERFACE_DISCOVERY_SCRIPT := interface_discovery.py
CV_API_MODULE := cv_api.py
NODE_INVENTORY_SERVICE := cv-node-inventory.service
SYSTEMD_DIR := /etc/systemd/system
SLURM_CONF := $(SLURM_CONF_DIR)/slurm.conf

.PHONY: help
help: ## Show this help message
	@echo "CloudVision Slurm Integration"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-25s %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables:"
	@echo "  API_SERVER           CloudVision API server (REQUIRED, e.g., www.arista.io)"
	@echo "  API_TOKEN            CloudVision API token (REQUIRED)"
	@echo ""
	@echo "To obtain API_SERVER and API_TOKEN, refer to:"
	@echo "  https://aristanetworks.github.io/cloudvision-apis/connecting"
	@echo "  SLURM_CONF_DIR       Slurm config directory (default: /etc/slurm)"
	@echo "  INSTALL_DIR          Installation directory (default: /opt/slurm/cloudvision)"
	@echo "  LOG_DIR              Log directory (default: /var/log/slurm)"
	@echo ""
	@echo "Example:"
	@echo "  make install API_SERVER=www.arista.io API_TOKEN=your-token"

.PHONY: install
install: ## Install CloudVision Slurm integration (requires API_SERVER, API_TOKEN)
	@test -n "$(API_SERVER)" || (echo "Error: API_SERVER is required. Usage: make install API_SERVER=... API_TOKEN=..." && exit 1)
	@test -n "$(API_TOKEN)" || (echo "Error: API_TOKEN is required. Usage: make install API_SERVER=... API_TOKEN=..." && exit 1)
	@command -v $(PYTHON) >/dev/null 2>&1 || (echo "Error: $(PYTHON) not found" && exit 1)
	@command -v $(PIP) >/dev/null 2>&1 || (echo "Error: $(PIP) not found" && exit 1)
	@command -v sinfo >/dev/null 2>&1 || (echo "Error: Slurm not found" && exit 1)
	@echo "=========================================="
	@echo "CloudVision Slurm Integration - Install"
	@echo "=========================================="
	@echo ""
	@echo "Configuration:"
	@echo "  API Server:         $(API_SERVER)"
	@echo "  Install Dir:        $(INSTALL_DIR)"
	@echo "  Log Dir:            $(LOG_DIR)"
	@echo "  Slurm Config:       $(SLURM_CONF)"
	@echo ""
	@echo "Note: Other settings (LOG_LEVEL, PARTITION_FILTER, IFACE_NAME_REGEX)"
	@echo "      can be modified directly in the installed scripts after installation."
	@echo ""
	@bash -c 'read -p "Proceed with installation? [y/N] " -n 1 -r; echo; if [[ ! $$REPLY =~ ^[Yy]$$ ]]; then echo "Cancelled"; exit 1; fi'
	@echo ""
	@echo "Step 1/4: Installing Python dependencies on controller..."
	@bash -c ' \
		read -p "  Install requests library on controller? [y/N] " -n 1 -r; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			$(PIP) install --user requests 2>/dev/null || $(PIP) install --user --break-system-packages requests 2>/dev/null; \
			echo "  ✓ Installed on controller"; \
		else \
			echo "  Skipped"; \
		fi'
	@echo ""
	@echo "Step 2/5: Creating directories and copying scripts..."
	@bash -c ' \
		read -p "  Create $(INSTALL_DIR) and $(LOG_DIR)? [y/N] " -n 1 -r; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo mkdir -p $(INSTALL_DIR) && echo "  ✓ Created $(INSTALL_DIR)"; \
			sudo mkdir -p $(LOG_DIR) && echo "  ✓ Created $(LOG_DIR)"; \
			sudo cp $(CV_API_MODULE) $(INSTALL_DIR)/ && echo "  ✓ Copied $(CV_API_MODULE)"; \
			sudo cp $(JOB_HOOK_SCRIPT) $(INSTALL_DIR)/ && echo "  ✓ Copied $(JOB_HOOK_SCRIPT)"; \
			sudo cp $(NODE_INVENTORY_SCRIPT) $(INSTALL_DIR)/ && echo "  ✓ Copied $(NODE_INVENTORY_SCRIPT)"; \
			sudo cp $(INTERFACE_DISCOVERY_SCRIPT) $(INSTALL_DIR)/ && echo "  ✓ Copied $(INTERFACE_DISCOVERY_SCRIPT)"; \
			sudo sed -i "s#API_SERVER = \"\"#API_SERVER = \"$(API_SERVER)\"#" $(INSTALL_DIR)/$(JOB_HOOK_SCRIPT); \
			sudo sed -i "s#API_TOKEN = \"\"#API_TOKEN = \"$(API_TOKEN)\"#" $(INSTALL_DIR)/$(JOB_HOOK_SCRIPT); \
			sudo sed -i "s#API_SERVER = \"\"#API_SERVER = \"$(API_SERVER)\"#" $(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT); \
			sudo sed -i "s#API_TOKEN = \"\"#API_TOKEN = \"$(API_TOKEN)\"#" $(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT); \
			sudo chmod +x $(INSTALL_DIR)/$(JOB_HOOK_SCRIPT) $(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT) $(INSTALL_DIR)/$(INTERFACE_DISCOVERY_SCRIPT) && echo "  ✓ Set executable permissions"; \
			echo "  ✓ Configured API_SERVER and API_TOKEN"; \
			echo "  ℹ Other settings (LOG_LEVEL, JOB_NAME_FILTER, PARTITION_FILTER, IFACE_NAME_REGEX) can be modified directly in:"; \
			echo "    - $(INSTALL_DIR)/$(JOB_HOOK_SCRIPT)"; \
			echo "    - $(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT)"; \
			echo "    - $(INSTALL_DIR)/$(INTERFACE_DISCOVERY_SCRIPT)"; \
		else \
			echo "  Skipped"; \
		fi'
	@echo ""
	@echo "Step 3/5: Configuring Slurm hooks..."
	@bash -c ' \
		read -p "  Add hooks to $(SLURM_CONF)? [y/N] " -n 1 -r; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo sed -i "/^PrologSlurmctld=/d" $(SLURM_CONF) 2>/dev/null || true; \
			sudo sed -i "/^EpilogSlurmctld=/d" $(SLURM_CONF) 2>/dev/null || true; \
			sudo sed -i "/# CloudVision Integration/d" $(SLURM_CONF) 2>/dev/null || true; \
			echo "# CloudVision Integration" | sudo tee -a $(SLURM_CONF) > /dev/null; \
			echo "PrologSlurmctld=$(INSTALL_DIR)/$(JOB_HOOK_SCRIPT)" | sudo tee -a $(SLURM_CONF) > /dev/null && echo "  ✓ Added PrologSlurmctld"; \
			echo "EpilogSlurmctld=$(INSTALL_DIR)/$(JOB_HOOK_SCRIPT)" | sudo tee -a $(SLURM_CONF) > /dev/null && echo "  ✓ Added EpilogSlurmctld"; \
		else \
			echo "  Skipped"; \
		fi'
	@echo ""
	@echo "Step 4/5: Installing node inventory systemd service..."
	@bash -c ' \
		read -p "  Install systemd service for automatic node monitoring? [y/N] " -n 1 -r; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo cp $(NODE_INVENTORY_SERVICE) $(SYSTEMD_DIR)/ && echo "  ✓ Copied $(NODE_INVENTORY_SERVICE) to $(SYSTEMD_DIR)"; \
			sudo systemctl daemon-reload && echo "  ✓ Reloaded systemd"; \
			sudo systemctl enable $(NODE_INVENTORY_SERVICE) && echo "  ✓ Enabled $(NODE_INVENTORY_SERVICE)"; \
			sudo systemctl start $(NODE_INVENTORY_SERVICE) && echo "  ✓ Started $(NODE_INVENTORY_SERVICE)"; \
			echo "  ℹ Monitor logs with: journalctl -u $(NODE_INVENTORY_SERVICE) -f"; \
		else \
			echo "  Skipped - node monitoring will not run automatically"; \
		fi'
	@echo ""
	@echo "Step 5/5: Restarting slurmctld..."
	@bash -c ' \
		read -p "  Restart slurmctld service? [y/N] " -n 1 -r; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo systemctl restart slurmctld && echo "  ✓ slurmctld restarted"; \
		else \
			echo "  Skipped - remember to restart manually!"; \
		fi'
	@echo ""
	@echo "=========================================="
	@echo "✓ Installation Complete!"
	@echo "=========================================="
	@echo ""
	@echo "Node inventory service status:"
	@bash -c 'systemctl is-active $(NODE_INVENTORY_SERVICE) >/dev/null 2>&1 && echo "  ✓ cv-node-inventory.service is running" || echo "  ⚠ cv-node-inventory.service is not running"'

.PHONY: uninstall
uninstall: ## Uninstall CloudVision Slurm integration
	@echo "=========================================="
	@echo "CloudVision Slurm Integration - Uninstall"
	@echo "=========================================="
	@echo ""
	@bash -c 'read -p "Remove all CloudVision Slurm integration files? [y/N] " -n 1 -r || exit 1; echo; [[ $$REPLY =~ ^[Yy]$$ ]] || (echo "Cancelled" && exit 1)'
	@echo ""
	@echo "Step 1/4: Stopping and removing node inventory service..."
	@bash -c ' \
		read -p "  Stop and remove $(NODE_INVENTORY_SERVICE)? [y/N] " -n 1 -r || exit 1; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo systemctl stop $(NODE_INVENTORY_SERVICE) 2>/dev/null && echo "  ✓ Stopped $(NODE_INVENTORY_SERVICE)" || echo "  ⚠ Service not running"; \
			sudo systemctl disable $(NODE_INVENTORY_SERVICE) 2>/dev/null && echo "  ✓ Disabled $(NODE_INVENTORY_SERVICE)" || echo "  ⚠ Service not enabled"; \
			sudo rm -f $(SYSTEMD_DIR)/$(NODE_INVENTORY_SERVICE) && echo "  ✓ Removed $(NODE_INVENTORY_SERVICE)"; \
			sudo systemctl daemon-reload && echo "  ✓ Reloaded systemd"; \
		else \
			echo "  Skipped"; \
		fi' || exit 1
	@echo ""
	@echo "Step 2/4: Removing Slurm hooks from $(SLURM_CONF)..."
	@bash -c ' \
		read -p "  Remove hooks from slurm.conf? [y/N] " -n 1 -r || exit 1; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo sed -i "/PrologSlurmctld.*cv-job-hook/d" $(SLURM_CONF) 2>/dev/null && echo "  ✓ Removed PrologSlurmctld" || echo "  ⚠ PrologSlurmctld not found"; \
			sudo sed -i "/EpilogSlurmctld.*cv-job-hook/d" $(SLURM_CONF) 2>/dev/null && echo "  ✓ Removed EpilogSlurmctld" || echo "  ⚠ EpilogSlurmctld not found"; \
		else \
			echo "  Skipped"; \
		fi' || exit 1
	@echo ""
	@echo "Step 3/4: Removing installed scripts..."
	@bash -c ' \
		read -p "  Remove $(INSTALL_DIR)? [y/N] " -n 1 -r || exit 1; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo rm -rf $(INSTALL_DIR) && echo "  ✓ Removed $(INSTALL_DIR)"; \
		else \
			echo "  Skipped"; \
		fi' || exit 1
	@echo ""
	@echo "Step 4/4: Restarting slurmctld..."
	@bash -c ' \
		read -p "  Restart slurmctld service? [y/N] " -n 1 -r || exit 1; \
		echo; \
		if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
			sudo systemctl restart slurmctld && echo "  ✓ slurmctld restarted"; \
		else \
			echo "  Skipped - remember to restart manually!"; \
		fi' || exit 1
	@echo ""
	@echo "=========================================="
	@echo "✓ Uninstall Complete!"
	@echo "=========================================="

.PHONY: update-node-inventory
update-node-inventory: ## Run node inventory collection once and send to CloudVision
	@test -f "$(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT)" || (echo "Error: $(NODE_INVENTORY_SCRIPT) not installed. Run 'make install' first." && exit 1)
	@echo "Running one-time node inventory collection..."
	@$(INSTALL_DIR)/$(NODE_INVENTORY_SCRIPT)

.DEFAULT_GOAL := help

# Catch-all rule for unknown targets
%:
	@echo "Error: Unknown target '$@'"
	@echo "Run 'make help' to see available targets"
	@exit 1

