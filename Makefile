# Build decklink-configure and decklink-status.
#
# Requires the Blackmagic DeckLink SDK headers. setup.sh installs them to
# /opt/decklink-sdk from Blackmagic_DeckLink_SDK_*.zip. Point DECKLINK_SDK at
# that directory, or at an SDK tree that still has Linux/include underneath.
#
#   make DECKLINK_SDK=/opt/decklink-sdk
#   sudo install -m 0755 decklink-configure decklink-status /usr/local/bin/

DECKLINK_SDK ?= /opt/decklink-sdk

CXX      ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra
LDLIBS   += -ldl -lpthread

define DECKLINK_BUILD
	@sdk='$(DECKLINK_SDK)'; \
	hdr="$$(find -L "$$sdk" -name DeckLinkAPI.h 2>/dev/null | head -n1)"; \
	dispatch="$$(find -L "$$sdk" -name DeckLinkAPIDispatch.cpp 2>/dev/null | head -n1)"; \
	if [ -z "$$hdr" ]; then echo "ERROR: DeckLinkAPI.h not found under $$sdk"; exit 1; fi; \
	if [ -z "$$dispatch" ]; then echo "ERROR: DeckLinkAPIDispatch.cpp not found under $$sdk"; exit 1; fi; \
	incdir="$$(dirname "$$hdr")"; \
	echo "Using headers:  $$incdir"; \
	echo "Using dispatch: $$dispatch"; \
	$(CXX) $(CXXFLAGS) -I"$$incdir" -o $(1) $(2) "$$dispatch" $(LDLIBS)
endef

decklink-configure: decklink-configure.cpp
	$(call DECKLINK_BUILD,decklink-configure,decklink-configure.cpp)

decklink-status: decklink-status.cpp
	$(call DECKLINK_BUILD,decklink-status,decklink-status.cpp)

.PHONY: decklink-configure decklink-status
