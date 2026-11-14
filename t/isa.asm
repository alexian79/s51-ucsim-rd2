; Deterministic ISA exercise for cross-validating two independent 8051 cores.
; Straight-line ALU/logic/data-move plus one of each control-flow form. No
; peripherals, no interrupts, ends in a self-loop. ACC holds the result of every
; interesting operation so the per-instruction trace is meaningful.

        .area CODE (ABS)
        .org 0x0000
        ljmp  main

        .org 0x0003
        reti
        .org 0x000b
        reti
        .org 0x0013
        reti

        .org 0x0030
main:
        mov   0x81, #0x60

; --- data movement -------------------------------------------------------
        mov   a, #0x5a
        mov   0x40, a
        mov   0x41, #0xa5
        mov   a, 0x41
        mov   r0, a
        mov   a, r0
        mov   r1, #0x20
        mov   @r1, a
        mov   a, @r1
        mov   0x42, r0
        mov   r3, 0x41
        mov   dptr, #0x1200
        movx  @dptr, a
        clr   a
        movx  a, @dptr

; --- arithmetic ----------------------------------------------------------
        mov   a, #0xff
        add   a, #0x02
        mov   a, #0x01
        addc  a, #0x01
        mov   a, #0x00
        subb  a, #0x01
        mov   a, #0x10
        inc   a
        dec   a
        inc   0x40
        dec   0x40
        mov   a, #0x0d
        mov   0xf0, #0x0a
        mul   ab
        mov   a, #0x82
        div   ab

; --- logic ---------------------------------------------------------------
        mov   a, #0xf0
        anl   a, #0x3c
        orl   a, #0x0f
        xrl   a, #0xff
        cpl   a
        rl    a
        rr    a
        rlc   a
        rrc   a
        swap  a
        mov   a, #0x99
        da    a
        anl   0x41, #0x0f
        orl   0x41, #0xf0
        mov   a, 0x41

; --- exchange ------------------------------------------------------------
        mov   r2, #0x77
        mov   a, #0x88
        xch   a, r2
        xchd  a, @r1
        mov   a, @r1

; --- stack ---------------------------------------------------------------
        push  0xe0
        push  0xf0
        push  0x83
        push  0x82
        pop   0x82
        pop   0x83
        pop   0xf0
        pop   0xe0

; --- calls ---------------------------------------------------------------
        lcall sub1
        acall sub2

; --- loops and compares --------------------------------------------------
        mov   r3, #0x04
loop1:
        djnz  r3, loop1
        mov   a, #0x07
        mov   r4, #0x00
loop2:
        inc   r4
        cjne  r4, #0x03, loop2

; --- unconditional and conditional branches ------------------------------
        mov   a, #0x00
        jz    taken
        sjmp  skip
taken:
        nop
skip:
        mov   a, #0x01
        jnz   taken2
        sjmp  skip2
taken2:
        cjne  a, #0x02, noteq
        sjmp  skip2
noteq:
        nop
skip2:
        setb  0xd5
        jb    0xd5, b1
        sjmp  b2
b1:
        nop
b2:
        cpl   0xd5
        jnb   0xd5, b3
        sjmp  b4
b3:
        nop
b4:
        clr   c
        cpl   c
        mov   c, 0xd5
        mov   0x43, c

; --- indexed jump --------------------------------------------------------
        mov   a, #0x00
        mov   dptr, #jt
        jmp   @a+dptr
jt:
        sjmp  done
        sjmp  done

done:
        sjmp  done

sub1:
        mov   a, #0x11
        add   a, #0x22
        ret

sub2:
        mov   a, #0x33
        addc  a, #0x01
        ret
