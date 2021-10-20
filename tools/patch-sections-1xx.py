#!/usr/bin/python

import argparse
import io
import lief
import os
import struct

# Indices
INDEX_NULL = 0
INDEX_INTERP = 1
INDEX_HASH = 2
INDEX_DYNSYM = 3
INDEX_DYNSTR = 4
INDEX_TEXT = 5
INDEX_DYNAMIC = 6
INDEX_SHSTRTAB = 7

# Utilities
class Section(object):
    def __init__(self):
        self.name_idx = 0
        self.type = lief.ELF.SECTION_TYPES.NULL
        self.flags = lief.ELF.SECTION_FLAGS.NONE
        self.virtual_address = 0
        self.offset = 0
        self.size = 0
        self.link = 0
        self.information = 0
        self.alignment = 0
        self.entry_size = 0
        # Helpers
        self.name = ""
        self.content = b""

    def serialize(self):
        section_fmt = 'IIQQQQIIQQ'
        assert struct.calcsize(section_fmt) == 0x40
        return struct.pack(section_fmt,
            self.name_idx,
            int(self.type),
            int(self.flags),
            self.virtual_address,
            self.offset,
            self.size,
            self.link,
            self.information,
            self.alignment,
            self.entry_size)

class Symbol(object):
    def __init__(self, data):
        symbol_fmt = 'IBBHQQ'
        assert struct.calcsize(symbol_fmt) == 0x18
        fields = struct.unpack(symbol_fmt, data)
        self.name = fields[0]
        self.info = fields[1]
        self.other = fields[2]
        self.shndx = fields[3]
        self.value = fields[4]
        self.size = fields[5]

def patch_i08(stream, offset, value):
    data = struct.pack('B', value)
    stream.seek(offset)
    stream.write(data)
    
def patch_i16(stream, offset, value):
    data = struct.pack('H', value)
    stream.seek(offset)
    stream.write(data)
    
def patch_i32(stream, offset, value):
    data = struct.pack('I', value)
    stream.seek(offset)
    stream.write(data)
    
def patch_i64(stream, offset, value):
    data = struct.pack('Q', value)
    stream.seek(offset)
    stream.write(data)

def read_i32(elf, addr):
    data = bytes(elf.get_content_from_virtual_address(addr, 4))
    value = struct.unpack('I', data)[0]
    return value

def get_load_segment(elf, index=0):
    for segment in elf.segments:
        if segment.type != lief.ELF.SEGMENT_TYPES.LOAD:
            continue
        if index == 0:
            return segment
        index -= 1
    raise Exception("Segment not found")

def get_dynamic_entry(elf, tag):
    for de in elf.dynamic_entries:
        if de.tag == tag:
            return de
    raise Exception("Segment not found")


# Sections
def create_section_null():
    return Section()


def create_section_interp(elf):
    section = Section()
    section.name = ".interp"
    section.type = lief.ELF.SECTION_TYPES.PROGBITS
    section.flags = lief.ELF.SECTION_FLAGS.ALLOC
    section.link = 0
    section.information = 0
    section.alignment = 1
    section.entry_size = 0
    section.size = len(elf.dynamic_entries) * section.entry_size
    
    # Assume it appears after Ehdr+Phdr's of the first LOAD segment
    hdr = elf.header
    assert hdr.header_size == hdr.program_header_offset
    segment = get_load_segment(elf, index=0)
    section.virtual_address = segment.virtual_address
    section.virtual_address += hdr.header_size
    section.virtual_address += hdr.numberof_segments * hdr.program_header_size
    section.offset = elf.virtual_address_to_offset(segment.virtual_address)
    # Assume it appears before the .hash section
    hash_vaddr = get_dynamic_entry(elf, lief.ELF.DYNAMIC_TAGS.HASH).value
    assert hash_vaddr > section.virtual_address
    section.size = hash_vaddr - section.virtual_address
    return section


def create_section_hash(elf):
    section = Section()
    section.name = ".hash"
    section.type = lief.ELF.SECTION_TYPES.HASH
    section.flags = lief.ELF.SECTION_FLAGS.ALLOC
    section.link = INDEX_DYNSYM
    section.information = 0
    section.alignment = 8
    section.entry_size = 4

    section.virtual_address = get_dynamic_entry(elf, lief.ELF.DYNAMIC_TAGS.HASH).value
    section.offset = elf.virtual_address_to_offset(section.virtual_address)
    hash_nbucket = read_i32(elf, section.virtual_address + 0x0)
    hash_nchain  = read_i32(elf, section.virtual_address + 0x4)
    section.size = 8 + (hash_nbucket + hash_nchain) * 4
    return section


def create_section_dynsym(elf):
    section = Section()
    section.name = ".dynsym"
    section.type = lief.ELF.SECTION_TYPES.DYNSYM
    section.flags = lief.ELF.SECTION_FLAGS.ALLOC
    section.link = INDEX_DYNSTR
    section.information = 0
    section.alignment = 8
    section.offset = 0
    section.entry_size = 0
    section.size = 0
    
    for de in elf.dynamic_entries:
        if de.tag == lief.ELF.DYNAMIC_TAGS.SYMTAB:
            section.virtual_address = de.value
            section.offset = elf.virtual_address_to_offset(de.value)
        if de.tag == lief.ELF.DYNAMIC_TAGS.SYMENT:
            section.entry_size = de.value     
    if not section.offset and not section.entry_size:
        raise Exception("No dynamic entries for symbols")
    assert section.entry_size == 0x18

    while True:
        symdata = bytes(elf.get_content_from_virtual_address(
            section.virtual_address + section.size,
            section.entry_size))
        sym = Symbol(symdata)
        if sym.size > 0xFFFFFFFF: break
        section.size += section.entry_size
    return section


def create_section_dynstr(elf):
    section = Section()
    section.name = ".dynstr"
    section.type = lief.ELF.SECTION_TYPES.STRTAB
    section.flags = lief.ELF.SECTION_FLAGS.ALLOC
    section.link = 0
    section.information = 0
    section.alignment = 1
    section.offset = 0
    section.entry_size = 0
    section.size = 0

    for de in elf.dynamic_entries:
        if de.tag == lief.ELF.DYNAMIC_TAGS.STRTAB:
            section.virtual_address = de.value
            section.offset = elf.virtual_address_to_offset(de.value)
        if de.tag == lief.ELF.DYNAMIC_TAGS.STRSZ:
            section.size = de.value

    if not section.offset and not section.size:
        raise Exception("No dynamic entries for strings")
    return section


def create_section_text(elf):
    section = Section()
    section.name = ".text"
    section.type = lief.ELF.SECTION_TYPES.PROGBITS
    section.flags = lief.ELF.SECTION_FLAGS.ALLOC | lief.ELF.SECTION_FLAGS.EXECINSTR
    section.link = 0
    section.information = 0
    section.alignment = 16
    section.entry_size = 0
    section.size = len(elf.dynamic_entries) * section.entry_size

    # TODO
    segment = get_load_segment(elf, index=0)
    section.virtual_address = segment.virtual_address
    section.offset = elf.virtual_address_to_offset(section.virtual_address)
    section.size = segment.virtual_size
    return section


def create_section_dynamic(elf):
    section = Section()
    section.name = ".dynamic"
    section.type = lief.ELF.SECTION_TYPES.DYNAMIC
    section.flags = lief.ELF.SECTION_FLAGS.WRITE | lief.ELF.SECTION_FLAGS.ALLOC
    section.link = INDEX_DYNSTR
    section.information = 0
    section.alignment = 8
    section.entry_size = 0x10
    section.size = len(elf.dynamic_entries) * section.entry_size

    # Assume .dynamic appears at the beginning of the second LOAD segment
    segment = get_load_segment(elf, index=1)
    section.virtual_address = segment.virtual_address
    section.offset = elf.virtual_address_to_offset(segment.virtual_address)
    return section


def create_section_shstrtab(sections):
    section = Section()
    section.name = ".shstrtab"
    section.type = lief.ELF.SECTION_TYPES.STRTAB
    section.flags = lief.ELF.SECTION_FLAGS.NONE
    section.link = 0
    section.information = 0
    section.alignment = 1
    section.offset = 0
    section.entry_size = 0
    section.size = 0

    section.content = b''
    for other_section in sections:
        other_section.name_idx = len(section.content)
        section.content += other_section.name.encode('ascii') + b'\x00'
    section.name_idx = len(section.content)
    section.content += section.name.encode('ascii') + b'\x00'
    return section
    

def patch_sections(path_in, path_out):
    elf = lief.parse(path_in)
    assert len(elf.sections) == 0, "Expected an executable without sections"
    sections = []
    sections.append(create_section_null())
    sections.append(create_section_interp(elf))
    sections.append(create_section_hash(elf))
    sections.append(create_section_dynsym(elf))
    sections.append(create_section_dynstr(elf))
    sections.append(create_section_text(elf))
    sections.append(create_section_dynamic(elf))
    sections.append(create_section_shstrtab(sections))

    e_shentsize = 0x40
    e_shoff = os.path.getsize(path_in)
    e_shnum = len(sections)
    e_shstrndx = INDEX_SHSTRTAB

    with open(path_out, 'wb') as f:
        with open(path_in, 'rb') as binary:
            f.write(binary.read())
        patch_i64(f, 0x28, e_shoff)
        patch_i08(f, 0x3A, e_shentsize)
        patch_i08(f, 0x3C, e_shnum)
        patch_i08(f, 0x3E, e_shstrndx)
        f.seek(0, io.SEEK_END)
        offset = e_shoff + (e_shnum * e_shentsize)
        for section in sections:
            if section.content:
                section.offset = offset
                section.size = len(section.content)
                offset += len(section.content)
            f.write(section.serialize())
        for section in sections:
            f.write(section.content)


def main():
    parser = argparse.ArgumentParser(
        description='Generate ELF sections from dynamic entries in PS4 1.xx kernels.')
    parser.add_argument('input',
        metavar='input.elf', help='Path to input file',
    )
    parser.add_argument('output',
        metavar='output.elf', help='Path to output file',
    )
    args = parser.parse_args()
    patch_sections(args.input, args.output)

if __name__ == '__main__':
    main()
