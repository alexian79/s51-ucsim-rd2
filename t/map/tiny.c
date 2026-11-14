/* Fixture for the modern .map layout: one object per address class. */
unsigned int code_near(void);
__xdata unsigned char xvar[3];
__xdata unsigned char xinit[2] = { 1, 2 };
__data unsigned char counter;
__code unsigned int table[2] = { 0x1234, 0x5678 };
__bit flag;

unsigned int
code_near(void)
{
    counter = table[0];
    flag = 1;
    xvar[0] = counter ^ xinit[1];
    return (unsigned int)xvar[0] + counter;
}

void
main(void)
{
    for (;;) {
        code_near();
    }
}
