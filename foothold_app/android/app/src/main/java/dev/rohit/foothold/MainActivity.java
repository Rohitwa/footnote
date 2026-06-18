package dev.rohit.foothold;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Register the on-device AI plugin before the bridge loads the web app,
        // so the remote UI can call OnDeviceAI.{status,embed,generate}.
        registerPlugin(OnDeviceAiPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
