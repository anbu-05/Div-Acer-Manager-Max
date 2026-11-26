# Kbuild makefile for the linuwu_sense driver.
# DKMS calls this with KERNELRELEASE set; "make" by hand builds for the
# running kernel (handy for a quick compile test, no install).
obj-m := src/linuwu_sense.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
