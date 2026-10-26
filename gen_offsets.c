// gen_offsets - print pcars2APIStruct field offsets as JSON for pcars2.py.
// Rebuild with `make offsets` if pcars2data.h changes. Every field of the
// struct is listed; pcars2.py's FIELDS gives each one's type and count.
#include <stddef.h>
#include <stdio.h>
#include "pcars2data.h"

#define F(name) printf("  \"%s\": %zu,\n", #name, offsetof(struct pcars2APIStruct, name))
#define P(name) printf("  \"p_%s\": %zu,\n", #name, offsetof(ParticipantInfo, name))

int main(void)
{
    printf("{\n");
    F(mVersion); F(mBuildVersionNumber); F(mGameState); F(mSessionState); F(mRaceState);
    F(mViewedParticipantIndex); F(mNumParticipants); F(mParticipantInfo);
    F(mUnfilteredThrottle); F(mUnfilteredBrake); F(mUnfilteredSteering); F(mUnfilteredClutch);
    F(mCarName); F(mCarClassName); F(mLapsInEvent); F(mTrackLocation); F(mTrackVariation); F(mTrackLength);
    F(mNumSectors); F(mLapInvalidated); F(mBestLapTime); F(mLastLapTime); F(mCurrentTime);
    F(mSplitTimeAhead); F(mSplitTimeBehind); F(mSplitTime); F(mEventTimeRemaining);
    F(mPersonalFastestLapTime); F(mWorldFastestLapTime);
    F(mCurrentSector1Time); F(mCurrentSector2Time); F(mCurrentSector3Time);
    F(mFastestSector1Time); F(mFastestSector2Time); F(mFastestSector3Time);
    F(mPersonalFastestSector1Time); F(mPersonalFastestSector2Time); F(mPersonalFastestSector3Time);
    F(mWorldFastestSector1Time); F(mWorldFastestSector2Time); F(mWorldFastestSector3Time);
    F(mHighestFlagColour); F(mHighestFlagReason); F(mPitMode); F(mPitSchedule);
    F(mCarFlags); F(mOilTempCelsius); F(mOilPressureKPa); F(mWaterTempCelsius); F(mWaterPressureKPa);
    F(mFuelPressureKPa); F(mFuelLevel); F(mFuelCapacity); F(mSpeed); F(mRpm); F(mMaxRPM);
    F(mBrake); F(mThrottle); F(mClutch); F(mSteering); F(mGear); F(mNumGears); F(mOdometerKM);
    F(mAntiLockActive); F(mLastOpponentCollisionIndex); F(mLastOpponentCollisionMagnitude);
    F(mBoostActive); F(mBoostAmount);
    F(mOrientation); F(mLocalVelocity); F(mWorldVelocity); F(mAngularVelocity);
    F(mLocalAcceleration); F(mWorldAcceleration); F(mExtentsCentre);
    F(mTyreFlags); F(mTerrain); F(mTyreY); F(mTyreRPS); F(mTyreSlipSpeed); F(mTyreTemp); F(mTyreGrip);
    F(mTyreHeightAboveGround); F(mTyreLateralStiffness); F(mTyreWear); F(mBrakeDamage);
    F(mSuspensionDamage); F(mBrakeTempCelsius); F(mTyreTreadTemp); F(mTyreLayerTemp);
    F(mTyreCarcassTemp); F(mTyreRimTemp); F(mTyreInternalAirTemp);
    F(mCrashState); F(mAeroDamage); F(mEngineDamage);
    F(mAmbientTemperature); F(mTrackTemperature); F(mRainDensity); F(mWindSpeed);
    F(mWindDirectionX); F(mWindDirectionY); F(mCloudBrightness);
    F(mSequenceNumber); F(mWheelLocalPositionY); F(mSuspensionTravel); F(mSuspensionVelocity);
    F(mAirPressure); F(mEngineSpeed); F(mEngineTorque); F(mWings); F(mHandBrake);
    F(mCurrentSector1Times); F(mCurrentSector2Times); F(mCurrentSector3Times);
    F(mFastestSector1Times); F(mFastestSector2Times); F(mFastestSector3Times);
    F(mFastestLapTimes); F(mLastLapTimes); F(mLapsInvalidated); F(mRaceStates); F(mPitModes);
    F(mOrientations); F(mSpeeds); F(mCarNames); F(mCarClassNames);
    F(mEnforcedPitStopLap); F(mTranslatedTrackLocation); F(mTranslatedTrackVariation);
    F(mBrakeBias); F(mTurboBoostPressure); F(mTyreCompound);
    F(mPitSchedules); F(mHighestFlagColours); F(mHighestFlagReasons); F(mNationalities); F(mSnowDensity);
    F(mSessionDuration); F(mSessionAdditionalLaps);
    F(mTyreTempLeft); F(mTyreTempCenter); F(mTyreTempRight); F(mDrsState); F(mRideHeight);
    F(mJoyPad0); F(mDPad); F(mAntiLockSetting); F(mTractionControlSetting);
    F(mErsDeploymentMode); F(mErsAutoModeEnabled);
    F(mClutchTemp); F(mClutchWear); F(mClutchOverheated); F(mClutchSlipping); F(mYellowFlagState);
    P(mIsActive); P(mName); P(mWorldPosition); P(mCurrentLapDistance); P(mRacePosition);
    P(mLapsCompleted); P(mCurrentLap); P(mCurrentSector);
    printf("  \"participant_size\": %zu,\n", sizeof(ParticipantInfo));
    printf("  \"total_size\": %zu\n}\n", sizeof(struct pcars2APIStruct));
    return 0;
}
