package com.example.hello;

import android.app.Activity;
import android.os.Bundle;

public class MainActivity extends Activity {
    public static final String GREETING = "Hello from MinApp";

    static int checksum(String text) {
        int sum = 0;
        for (int i = 0; i < text.length(); i++) {
            sum = sum * 31 + text.charAt(i);
        }
        return sum;
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setTitle(GREETING + " #" + checksum(GREETING));
    }
}
